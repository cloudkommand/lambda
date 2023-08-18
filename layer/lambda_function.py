import boto3
import botocore
from botocore.exceptions import ClientError

import json
import os
import subprocess
import tempfile
import traceback
import zipfile
import hashlib

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, create_zip, \
    handle_common_errors, random_id, lambda_env

ALLOWED_RUNTIMES = [
    "python3.10", "python3.9", "python3.8", "python3.7", 
    "nodejs14.x", "nodejs12.x", "nodejs18.x",
    "ruby2.7", "nodejs16.x", "go1.x",
    "dotnet6", "dotnet5.0", "java11",
    "java8", "java8.al2"
]

REQUIRED_PROPS = [
    "name", "arn", "def_hash", "code_sha", "version_arn", "version",
    "initial_etag"
]
REQUIRED_LINKS = [
    "Layer"
]
REQUIRED_ARTIFACTS = [
    "code"
]

CODEBUILD_PROJECT_KEY = "Codebuild Project"
CODEBUILD_BUILD_KEY = "Codebuild Build"

eh = ExtensionHandler()

lambda_client = boto3.client("lambda")
s3 = boto3.client("s3")
def lambda_handler(event, context):
    #Three types of trust:
    # 1. Zero
    # 2. Code
    # 3. Full
    try:
        print(f"event = {event}")
        # account_number = account_context(context)['number']
        region = account_context(context)['region']
        eh.capture_event(event)
        prev_state = event.get("prev_state")
        cdef = event.get("component_def")
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cname = event.get("component_name")

        trust_level = cdef.get("trust_level") or "code"
        rollback = event.get("rollback")
        if rollback:
            trust_level = "zero"

        layer_name = cdef.get("name") or component_safe_name(project_code, repo_id, cname)
        runtime = cdef.get("requirements_runtime") or "python3.9"
        # if requirements_runtime and requirements_runtime not in ALLOWED_RUNTIMES:
        #     eh.perm_error("requirements_runtime invalid", {"runtime": requirements_runtime, "allowed": ALLOWED_RUNTIMES})
        
        codebuild_project_override_def = cdef.get(CODEBUILD_PROJECT_KEY) or {} #For codebuild project overrides
        codebuild_build_override_def = cdef.get(CODEBUILD_BUILD_KEY) or {} #For codebuild build overrides

        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")

        pass_back_data = event.get("pass_back_data", {})
        if pass_back_data:
            pass
        elif event.get("op") == "upsert":
            eh.add_op("check_required_attributes")
            add_non_rollback_ops = True
            old_artifacts = prev_state.get("##artifacts##", {})
            # Simple Rollback using ##artifacts## key
            if rollback and old_artifacts and old_artifacts.get("code"):
                object_name = old_artifacts.get("code").get("location")
                try:
                    s3.get_object(Bucket=bucket, Key=object_name)
                    eh.add_op("publish_layer_version")
                    if prev_state and prev_state.get("props") and (prev_state.get("props").get("name")) != layer_name:
                        eh.add_op("remove_layer_versions", {"name":prev_state.get("props").get("name")})
                    eh.add_op("get_rollback_props")
                    add_non_rollback_ops = False
                except Exception as e:
                    print(str(e))
                    eh.add_log("Missing Rollback Code", {"error": str(e)}, is_error=True)


            if add_non_rollback_ops:
                if trust_level in ["full", "code"]:
                    eh.add_op("compare_defs")
                # elif trust_level == "code":
                #     eh.add_op("compare_etags")

                eh.add_op("load_initial_props")
                if cdef.get("requirements"):
                    eh.add_op("add_requirements", cdef.get("requirements"))
                    eh.add_state({"requirements": cdef.get("requirements")})
                elif cdef.get("requirements.txt"):
                    eh.add_op("add_requirements", "$$file")
                    eh.add_state({"requirements": "$$file"})
                    
                if prev_state and prev_state.get("props") and (prev_state.get("props").get("name")) != layer_name:
                    eh.add_op("remove_layer_versions", {"name":prev_state.get("props").get("name")})
                    eh.add_op("publish_layer_version")
                elif prev_state:
                    eh.add_op("check_if_update_required")
                else:
                    eh.add_op("publish_layer_version")

        elif event.get("op") == "delete":
            if prev_state.get("props", {}).get(CODEBUILD_PROJECT_KEY):
                eh.add_op("setup_codebuild_project")
            eh.add_op("remove_layer_versions", {"name": layer_name})

        #Elevated Trust Functions (Full, Code)
        compare_defs(event)
        check_code_sha(event, context)
        compare_etags(event, bucket, object_name)

        # Sorts out Props
        load_initial_props(bucket, object_name, event, context)


        add_requirements(context, runtime)

        #Python hack that reduces time to build by 2-3 fold.
        write_requirements_lambda_to_s3(bucket, runtime)
        deploy_requirements_lambda(bucket, runtime)
        invoke_requirements_lambda(bucket, object_name)
        check_requirements_built(bucket)
        remove_requirements_lambda(bucket, runtime)

        #All other runtimes that require building:
        setup_codebuild_project(bucket, object_name, codebuild_project_override_def, runtime, event["op"])
        run_codebuild_build(codebuild_build_override_def)

        #Now dealing with the layer itself
        check_if_update_required(prev_state, bucket, eh.state.get("new_object_name") or object_name)
        publish_layer_version(layer_name, cdef, bucket, eh.state.get("new_object_name") or object_name, region, runtime)
        remove_layer_versions(event.get("op"))

        #Wrapup
        check_required_attributes()

        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

def get_s3_etag(bucket, object_name):
    s3 = boto3.client("s3")

    try:
        s3_metadata = s3.head_object(Bucket=bucket, Key=object_name)
        print(f"s3_metadata = {s3_metadata}")
        eh.add_state({"zip_etag": s3_metadata['ETag']})
    except s3.exceptions.NoSuchKey:
        eh.add_log("Cound Not Find Zipfile", {"bucket": bucket, "key": object_name})
        eh.retry_error("Object Not Found")

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_digest = event.get("prev_state", {}).get("props", {}).get("def_hash")
    new_rendef = event.get("component_def")

    _ = new_rendef.pop("trust_level", None)

    dhash = hashlib.md5()
    dhash.update(json.dumps(new_rendef, sort_keys=True).encode())
    digest = dhash.hexdigest()
    eh.add_props({"def_hash": digest})

    if old_digest == digest:
        eh.add_log("Definitions Match, Checking Deploy Code", {"old_hash": old_digest, "new_hash": digest})
        eh.add_op("check_code_sha") 

    else:
        eh.add_log("Definitions Don't Match, Deploying", {"old": old_digest, "new": digest})

@ext(handler=eh, op="check_code_sha")
def check_code_sha(event, context):
    old_props = event.get("prev_state", {}).get("props", {})
    old_sha = old_props.get("code_sha")
    try:
        new_sha = lambda_client.get_function(
            FunctionName=context.function_name
        ).get("Configuration", {}).get("CodeSha256")
        eh.add_props({"code_sha": new_sha})
    except ClientError as e:
        handle_common_errors(e, eh, "Get Layer Function Failed", 2)
        
    if old_sha == new_sha:
        eh.add_log("Deploy Code Matches, Checking Code", {"old_sha": old_sha, "new_sha": new_sha})
        eh.add_op("compare_etags") 
    
    else:
        eh.add_log("Deploy Code Doesn't Match, Deploying", {"old_sha": old_sha, "new_sha": new_sha})


@ext(handler=eh, op="compare_etags")
def compare_etags(event, bucket, object_name):
    prev_state = event.get("prev_state", {})
    old_props = prev_state.get("props", {})

    initial_etag = old_props.get("initial_etag")

    #Get new etag
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        new_etag = eh.state["zip_etag"]
        if initial_etag == new_etag:
            eh.add_log("Elevated Trust: No Change Detected", {"initial_etag": initial_etag, "new_etag": new_etag})
            wrap_up_not_deploying(prev_state)

        else:
            eh.add_log("Code Changed, Deploying", {"old_etag": initial_etag, "new_etag": new_etag})

@ext(handler=eh, op="load_initial_props")
def load_initial_props(bucket, object_name, event, context):
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        eh.add_props({"initial_etag": eh.state.get("zip_etag")})

    if not eh.props.get("def_hash"):
        new_rendef = event.get("component_def")

        _ = new_rendef.pop("trust_level", None)

        dhash = hashlib.md5()
        dhash.update(json.dumps(new_rendef, sort_keys=True).encode())
        digest = dhash.hexdigest()
        eh.add_props({"def_hash": digest})

    if not eh.props.get("code_sha"):
        try:
            new_sha = lambda_client.get_function(
                FunctionName=context.function_name
            ).get("Configuration", {}).get("CodeSha256")
            eh.add_props({"code_sha": new_sha})
        except ClientError as e:
            handle_common_errors(e, eh, "Get Layer Function Failed", 2)
        

@ext(handler=eh, op="add_requirements")
def add_requirements(context, runtime):
    if runtime.startswith("python"):
        try:
            response = lambda_client.get_function(
                FunctionName=context.function_name
            )
            role_arn = response["Configuration"]["Role"]
            eh.add_state({"this_role_arn": role_arn})
            eh.add_op("write_requirements_lambda_to_s3")
        
        except ClientError as e:
            handle_common_errors(e, eh, "Get Requirements Role Failed", 25)
    else:
        eh.add_op("setup_codebuild_project")

@ext(handler=eh, op="write_requirements_lambda_to_s3")
def write_requirements_lambda_to_s3(bucket, runtime):

    requirements_lambda_name = random_id()
    requirements_object_name = f"requirements/{random_id()}"
    eh.add_state({
        "requirements_lambda_name": requirements_lambda_name,
        "requirements_object_name": requirements_object_name
    })

    if runtime.startswith("python"):
        function_code = """

import tempfile
import boto3
import datetime
import time
import os
import zipfile
import subprocess
import json

def lambda_handler(event, context):
    try:
        print(event)
        cdef = event.get("component_def")
        s3_key = event.get("s3_object_name")
        status_key = cdef.get("status_key")
        bucket = event.get("bucket")
        requirements = cdef.get("requirements")

        s3 = boto3.client("s3")
        with tempfile.TemporaryDirectory() as tmpdir:
            data = s3.get_object(Bucket=bucket, Key=s3_key)["Body"]
            filename = f"{tmpdir}/file.zip"
            with open(filename, "wb") as f:
                f.write(data.read())
            
            install_directory = f"{tmpdir}/install/"
            os.mkdir(install_directory)
            os.chdir(install_directory)
            with zipfile.ZipFile(filename, 'r') as archive:
                archive.extractall()

            requirements_file = f"{install_directory}requirements.txt"
            if requirements != "$$file":
                with open(requirements_file, "w") as f:
                    f.writelines("%s\\n" % i for i in requirements)
            
            if os.path.exists(requirements_file):
                with open(requirements_file, "r") as f:
                    print(f.read())
                dirs = next(os.walk('.'))[1]
                if not dirs:
                    print("No Folder")
                    os.mkdir("python")
                    dirs = ["python"]
                print(dirs)
                #We are going to assume there is only one directory, as there should be
                
                try:
                    subprocess.check_output(f'pip install -r requirements.txt -t ./{dirs[0]}'.split(), stderr=subprocess.STDOUT)                
                except subprocess.CalledProcessError as e:
                    raise Exception(f"Requirements Installation Failed: {e.output}")
            else:
                raise Exception("No requirements file found, please add a requirements.txt file or remove the 'requirements.txt' parameter")

            zipfile_name = f"{tmpdir}/file2.zip"
            create_zip(zipfile_name, install_directory[:-1])

            response = s3.upload_file(zipfile_name, bucket, s3_key)
            
        success = {"value": "success"}
        s3.put_object(
            Body=json.dumps(success),
            Bucket=bucket,
            Key=status_key
        )

    except Exception as e:
        error = {"value": str(e)}
        print(error)
        s3.put_object(
            Body=json.dumps(error),
            Bucket=bucket,
            Key=status_key
        )        
        
def create_zip(file_name, path):
    ziph=zipfile.ZipFile(file_name, 'w', zipfile.ZIP_DEFLATED)
    # ziph is zipfile handle
    for root, dirs, files in os.walk(path):
        for file in files:
            ziph.write(os.path.join(root, file), 
                       os.path.relpath(os.path.join(root, file), 
                                       os.path.join(path, '')))
    ziph.close()

def defaultconverter(o):
    if isinstance(o, datetime.datetime):
        return o.__str__()
"""
    

    s3 = boto3.client("s3")
    with tempfile.TemporaryDirectory() as tmpdir:
        os.mkdir(f"{tmpdir}/install")
        filename = f"{tmpdir}/install/lambda_function.py"
        with open(filename, "w+") as f:
            f.writelines(function_code)

        zipfile_name = f"{tmpdir}/file.zip"
        os.chdir(f"{tmpdir}/install")
        create_zip(zipfile_name, f"{tmpdir}/install")
        
        try:
            response = s3.upload_file(zipfile_name, bucket, requirements_object_name)
            eh.add_log("Requirements Lambda to S3", response)
        except boto3.exceptions.S3UploadFailedError:
            eh.add_log("Requirements Lambda to S3 Failed", {"zipfile_name": zipfile_name})
            eh.retry_error("S3 Upload Error for Requirements Lambda", 25)
        except ClientError as e:
            handle_common_errors(e, eh, "Requirements Lambda to S3 Failed", 25)

    eh.add_op("deploy_requirements_lambda")

@ext(handler=eh, op="deploy_requirements_lambda")
def deploy_requirements_lambda(bucket, runtime):
    eh.add_state({"status_key": random_id()})

    component_def = {
        "name": eh.state["requirements_lambda_name"],
        "role_arn": eh.state["this_role_arn"],
        "runtime": runtime,
        "memory_size": 2048,
        "timeout": 60,
        "description": "Temporary Lambda for adding requirements, will be removed",
    }

    eh.invoke_extension(
        arn=lambda_env("function_lambda_name"), component_def=component_def, 
        object_name=eh.state["requirements_object_name"],
        child_key="Requirements Lambda", progress_start=25, progress_end=30,
        op="upsert", links_prefix="Requirements"
    )

    eh.links.pop("Requirements Function", None)

    eh.add_op("invoke_requirements_lambda")

@ext(handler=eh, op="invoke_requirements_lambda")
def invoke_requirements_lambda(bucket, object_name):

    component_def = {
        "requirements": eh.state["requirements"],
        "status_key": eh.state["status_key"]
    }

    proceed = eh.invoke_extension(
        arn=eh.state["requirements_lambda_name"], component_def=component_def, 
        object_name=object_name,
        child_key="Requirements", progress_start=30, progress_end=35,
        op="upsert", ignore_props_links=True, synchronous=False
    )

    eh.add_op("check_requirements_built")


@ext(handler=eh, op="check_requirements_built")
def check_requirements_built(bucket):
    try:
        response = s3.get_object(Bucket=bucket, Key=eh.state["status_key"])['Body']
        value = json.loads(response.read()).get("value")
        eh.add_op("remove_requirements_lambda")
        if value == "success":
            eh.add_log("Requirements Built", value)
        else:
            eh.add_log(f"Requirements Errored", value, True)
            eh.add_state({"requirements_failed": value})

    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ['NoSuchKey']:
            eh.add_log("Build In Progress", {"error": None})
            eh.retry_error("Build In Progress", progress=35)
            # eh.add_log("Check Build Failed", {"error": str(e)}, True)
            # eh.perm_error(str(e), progress=65)
        else:
            eh.add_log("Check Build Error", {"error": str(e)}, True)
            eh.retry_error(str(e), progress=35)    


@ext(handler=eh, op="remove_requirements_lambda")
def remove_requirements_lambda(bucket, runtime):

    component_def = {
        "name": eh.state["requirements_lambda_name"],
        "role_arn": eh.state["this_role_arn"],
        "runtime": runtime,
        "memory_size": 2048,
        "timeout": 60,
        "description": "Temporary Lambda for adding requirements, will be removed",
    }

    eh.invoke_extension(
        arn=lambda_env("function_lambda_name"), component_def=component_def, 
        object_name=eh.state["requirements_object_name"],
        child_key="Requirements Lambda", progress_start=35, progress_end=40,
        op="delete", ignore_props_links=True
    )

    if eh.state.get("requirements_failed"):
        eh.perm_error(eh.state.get("requirements_failed"), progress=40)


@ext(handler=eh, op="setup_codebuild_project")
def setup_codebuild_project(bucket, object_name, codebuild_def, runtime, op):
    if not eh.state.get("codebuild_object_key"):
        eh.add_state({"codebuild_object_key": f"{random_id()}.zip"})

    runtime_version = LAMBDA_RUNTIME_TO_CODEBUILD_RUNTIME[runtime]
    pre_build_commands, build_commands, post_build_commands, buildspec_artifacts = get_default_buildspec_params(runtime)
    container_image = CODEBUILD_RUNTIME_TO_IMAGE_MAPPING[
        f"{list(runtime_version.keys())[0]}{list(runtime_version.values())[0]}"
    ]

    component_def = {
        "s3_bucket": bucket,
        "s3_object": object_name,
        "runtime_versions": runtime_version,
        "pre_build_commands": pre_build_commands,
        "build_commands": build_commands,
        "post_build_commands": post_build_commands,
        "buildspec_artifacts": buildspec_artifacts,
        "artifacts": {
            "type": "S3",
            "location": bucket,
            "path": "codebuildlambdas", 
            "name": eh.state["codebuild_object_key"],
            "packaging": "ZIP",
            "encryptionDisabled": True
        },
        "container_image": container_image
    }

    #Allows for custom overrides as the user sees fit
    component_def.update(codebuild_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_project_lambda_name"), 
        component_def=component_def, 
        child_key=CODEBUILD_PROJECT_KEY, progress_start=25, 
        progress_end=30
    )

    eh.add_state({"new_object_name": f"codebuildlambdas/{eh.state['codebuild_object_key']}"})

    if op == "upsert":
        eh.add_op("run_codebuild_build")

@ext(handler=eh, op="run_codebuild_build")
def run_codebuild_build(codebuild_build_def):
    print(eh.props)
    print(eh.links)

    component_def = {
        "project_name": eh.props[CODEBUILD_PROJECT_KEY]["name"]
    }

    component_def.update(codebuild_build_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_build_lambda_name"),
        component_def=component_def, 
        child_key=CODEBUILD_BUILD_KEY, progress_start=30, 
        progress_end=45
    )


@ext(handler=eh, op="check_if_update_required")
def check_if_update_required(prev_state, bucket, object_name):
    lambda_client = boto3.client("lambda")

    props = prev_state.get("props") or {}
    if not props.get("version_arn"):
        eh.add_op("publish_layer_version")

    else:
        try:
            layer_response = lambda_client.get_layer_version_by_arn(props['version_arn'])
            eh.add_log("Got Existing Layer Version", layer_response)
            code_etag = layer_response.get("description")
            print(f"code_etag = {code_etag}")
        except ClientError as e:
            print(str(e))
            if e.response['Error']['Code'] in ["ResourceNotFoundException", "ResourceNotFound"]:
                eh.add_log("Expected Layer Version Does Not Exist", {'layer_name': props.get("name")})
                eh.add_op("publish_layer_version")
                return None
            else:
                eh.add_log("Error in Getting Layer Version", {'layer_name': props.get("name")})
                eh.retry_error(e.response['Error']['Code'])
                return None
    
        get_s3_etag(bucket, object_name)
        if eh.state.get('zip_etag'):
            if code_etag == eh.state['zip_etag']:
                eh.add_log("No Update Needed", {"etag": eh.state.get('zip_etag')})
                eh.add_props(prev_state.get("props"))
                eh.add_links(prev_state.get("links"))
                eh.add_state(prev_state.get("state"))
                eh.add_artifacts(prev_state.get("artifacts"))
            else:
                eh.add_log("New Layer Version Required", {"etag": eh.state.get('zip_etag'), "code_etag": code_etag})
                eh.add_op("publish_layer_version")
        else:
            #Retry error already handled by get_s3_etag
            return None


@ext(handler=eh, op="publish_layer_version")
def publish_layer_version(layer_name, cdef, bucket, object_name, region, runtime):
    lambda_client = boto3.client("lambda")
    if not eh.state.get("zip_etag"):
        get_s3_etag(bucket, object_name)
    
    description = eh.state.get("zip_etag")
    compatible_runtimes = cdef.get("compatible_runtimes") or [
        "python3.9", "python3.8", "python3.6", "python3.7"
    ] if runtime.startswith("python") else [runtime]
    if not isinstance(compatible_runtimes, list):
        eh.perm_error("compatible_runtimes must be a list of strings")
    
    desired_config = remove_none_attributes({
        "LayerName": layer_name,
        "Description": description,
        "Content": {
            "S3Bucket": bucket,
            "S3Key": object_name
        },
        "CompatibleRuntimes": compatible_runtimes,
    })
    print(f"Inside publish_layer_version, desired_config = {desired_config}")

    try:
        lambda_response = lambda_client.publish_layer_version(
            **desired_config
        )
        eh.add_log("Published Layer Version", lambda_response)
        eh.add_props({
            "arn": lambda_response['LayerArn'],
            "version_arn": lambda_response['LayerVersionArn'],
            "version": lambda_response['Version']
        })
        eh.add_links({"Layer": gen_layer_link(layer_name, region)})
        eh.add_artifacts({"code": {"type": "S3", "location": object_name}})
        if lambda_response['Version'] != 1:
            eh.add_op("remove_layer_versions", {"name": layer_name, "version": lambda_response['Version']})

    except ClientError as e:
        print(str(e))
        if e.response['Error']['Code'] in ["InvalidParameterValueException", "CodeStorageExceededException"]:
            eh.add_log("Failure to Publish Layer Version", {'config': desired_config})
            eh.perm_error(str(e))
        else:
            eh.add_log("Error in Publishing Layer Version", {'layer_name':layer_name})
            eh.retry_error(e.response['Error']['Code'])


@ext(handler=eh, op="remove_layer_versions")
def remove_layer_versions(op):
    lambda_client = boto3.client("lambda")

    layer_name = eh.ops['remove_layer_versions']['name']
    safe_version = eh.ops['remove_layer_versions'].get("version")

    try:
        first = True
        all_layer_versions = []
        marker=None
        while first or marker:
            first = False
            layer_versions_retval = lambda_client.list_layer_versions(
                **remove_none_attributes({
                    "LayerName": layer_name,
                    "Marker": marker
                })
            )            
            # eh.add_log("Listed Layer Versions", layer_versions_retval)

            marker = layer_versions_retval.get("NextMarker")
            layer_versions = layer_versions_retval.get("LayerVersions")
            all_layer_versions.extend(layer_versions)

    except ClientError as e:
        if e.response['Error']['Code'] in ['ResourceNotFoundException', 'ResourceNotFound']:
            eh.add_log("Layer Does Not Exist", {"layer_name": layer_name})
            return None
        else:
            eh.retry_error("Listing Layer Versions", 80 if op == "upsert" else 0)

    for layer_version in layer_versions:
        try:
            if layer_version.get("Version") != safe_version:
                print(f"layer_version: {layer_version}")
                delete_retval = lambda_client.delete_layer_version(LayerName=layer_name, VersionNumber = layer_version.get("Version"))
                eh.add_log("Deleted Layer Version", layer_version)
        except Exception as e:
            print(str(e))

@ext(handler=eh)
def check_required_attributes():
    for prop in REQUIRED_PROPS:
        if not eh.props.get(prop):
            eh.add_log(f"Missing Required Prop: {prop}", {"props": eh.props}, True)
    for link in REQUIRED_LINKS:
        if not eh.links.get(link):
            eh.add_log(f"Missing Required Link: {link}", {"links": eh.links}, True)
    for artifact in REQUIRED_ARTIFACTS:
        if not eh.artifacts.get(artifact):
            eh.add_log(f"Missing Required Artifact: {artifact}", {"artifacts": eh.artifacts}, True)
    
def wrap_up_not_deploying(prev_state):
    eh.add_props(prev_state.get("props", {}))
    eh.add_links(prev_state.get("links", {}))
    eh.add_state(prev_state.get("state", {}))
    eh.add_artifacts(prev_state.get("artifacts", {}))
    eh.declare_return(200, 100, success=True)

def gen_layer_link(layer_name, region):
    return f"https://console.aws.amazon.com/lambda/home?region={region}#/layers/{layer_name}"

def get_default_buildspec_params(runtime):
    pre_build_commands = []
    build_commands = []
    post_build_commands = []
    buildspec_artifacts = {
        "files": [
            "**/*"
        ],
        # "base-directory": "target"
    }
    if runtime.startswith("node"):
        pre_build_commands = [
            "rm -rf node_modules",
        ]
        build_commands = [
            "echo 'Installing NPM Dependencies'",
            "npm install --production"
        ]
        post_build_commands = [
            "mkdir -p nodejs",
            "mv node_modules nodejs/"
        ]

    return pre_build_commands, build_commands, post_build_commands, buildspec_artifacts
   

LAMBDA_RUNTIME_TO_CODEBUILD_RUNTIME = {
    "nodejs18.x": {"nodejs": 18},
    "nodejs16.x": {"nodejs": 16},
    "nodejs14.x": {"nodejs": 14},
    "nodejs12.x": {"nodejs": 12},
    "java11": {"javacorretto": 11},
    "java8.al2": {"javacorretto": 8},
    "java8": {"javacorretto": 8},
    "dotnetcore3.1": {"dotnet": 3.1},
    "dotnet6": {"dotnet": 6},
    "dotnet5.0": {"dotnet": 5},
    "go1.x": {"golang": 1.16},
    "ruby2.7": {"ruby": 2.7},
}


CODEBUILD_RUNTIME_TO_IMAGE_MAPPING = {
    "android28": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "android29": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "dotnet3.1": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "dotnet5.0": "aws/codebuild/standard:5.0",
    "dotnet6.0": "aws/codebuild/standard:6.0",
    "golang1.12": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "golang1.13": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "golang1.14": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "golang1.15": "aws/codebuild/standard:5.0",
    "golang1.16": "aws/codebuild/standard:5.0",
    "golang1.18": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
    "javacorretto8": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "javacorretto11": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "javacorretto17": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
    "nodejs8": "aws/codebuild/amazonlinux2-aarch64-standard:1.0",
    "nodejs10": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "nodejs12": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "nodejs14": "aws/codebuild/standard:5.0",
    "nodejs16": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
    "php7.3": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "php7.4": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "php8.0": "aws/codebuild/standard:5.0",
    "php8.1": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
    "python3.7": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "python3.8": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "python3.9": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "python3.10": "aws/codebuild/standard:6.0",
    "ruby2.6": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "ruby2.7": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
    "ruby3.1": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
}