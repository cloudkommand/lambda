import boto3
import botocore
from botocore.exceptions import ClientError
# import jsonschema
import json
import os
import traceback
import subprocess
import zipfile
import tempfile
import base64
import hashlib

from extutil import remove_none_attributes, gen_log, creturn, handle_common_errors, \
    account_context, component_safe_name, ExtensionHandler, ext, lambda_env, \
    random_id, create_zip

eh = ExtensionHandler()
ALLOWED_RUNTIMES = ["python3.9", "python3.8", "python3.6", "python3.7", "nodejs14.x", "nodejs12.x", "nodejs10.x", "ruby2.7", "ruby2.5"]


lambda_client = boto3.client("lambda")
s3 = boto3.client("s3")
def lambda_handler(event, context):
    try:
        print(event)
        eh.capture_event(event)

        region = account_context(context)['region']
        account_number = account_context(context)['number']
        prev_state = event.get("prev_state") or {}
        op = event.get("op")

        cdef = event.get("component_def")
        cname = event.get("component_name")

        bucket = event.get("bucket")
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        object_name = event.get("s3_object_name")
        runtime = cdef.get("runtime") or "python3.9"
        if runtime not in ALLOWED_RUNTIMES:
            return creturn(200, 0, error=f"runtime {runtime} not allowed, please choose one of {ALLOWED_RUNTIMES}")

        handler = cdef.get("handler") or get_default_handler(runtime)
        description = cdef.get("description") or f"Lambda for component {cname}"
        timeout = cdef.get("timeout") or 5
        memory_size = cdef.get("memory_size") or 256
        environment = {"Variables": {k: str(v) for k,v in cdef.get("environment_variables").items()}} if cdef.get("environment_variables") else None
        trust_level = cdef.get("trust_level") or "code"

        tags = cdef.get("tags") or {}
        role = cdef.get("role", {})
        # if role and (not "lambda" in role.get("role_services", [])) and (not "lambda.amazonaws.com" in role.get("role_services", [])):
        #     return creturn(200, 0, error=f"The referenced role must have lambda in its list of trusted services")

        role_arn = cdef.get("role_arn") or role.get("arn")
        if role_arn:
            eh.add_state({"role_arn": role_arn})
        # elif prev_state.get("props", {}).get("Role", {}).get("arn"):
        #     eh.add_state({"role_arn": prev_state.get("props", {}).get("Role", {}).get("arn")})
        # if not role_arn:
        #     return creturn(200, 0, error=f"Must provide a role_arn. Please use either the role or role_arn keywords")

        policies = cdef.get("policies")
        policy_arns = cdef.get("policy_arns")
        role_description = "Created by CK for the Lambda function of the same name"
        role_tags = tags if cdef.get("also_tag_role") else cdef.get("role_tags")

        codebuild_project_override_def = cdef.get("Codebuild Project") or {} #For codebuild project overrides
        codebuild_build_override_def = cdef.get("Codebuild Build") or {} #For codebuild build overrides

        layer_arns = cdef.get("layer_version_arns") or []
        layers = cdef.get("layers") or []
        if layers:
            try:
                layer_arns.extend(list(map(lambda x: x['version_arn'], layers)))
            except:
                eh.add_log("Invalid Layer Parameters", {"layers": layers})
                eh.perm_error("Invalid layer parameters", 0)

        function_name = cdef.get("name") or component_safe_name(project_code, repo_id, cname)
        pass_back_data = event.get("pass_back_data", {})

        if pass_back_data:
            pass
        elif op == "upsert":
            if trust_level == "full":
                eh.add_op("compare_defs")

            eh.add_op("load_initial_props")
            eh.add_op('upsert_role')
            eh.add_op("get_lambda")
            eh.add_op("gen_props")
            if cdef.get("requirements"):
                eh.add_op("add_requirements")
                eh.add_state({"requirements": cdef.get("requirements")})
            elif cdef.get("requirements.txt"):
                eh.add_op("add_requirements")
                eh.add_state({"requirements": "$$file"})
        elif op == "delete":
            eh.add_op('remove_role')
            eh.add_op("remove_old", {"name": function_name})

        compare_defs(event)
        compare_etags(event, bucket, object_name)
        load_initial_props(bucket, object_name)

        upsert_role(prev_state, policies, policy_arns, role_description, role_tags)

        desired_config = remove_none_attributes({
            "FunctionName": function_name,
            "Description": description,
            "Handler": handler,
            "Role": eh.state['role_arn'] if op == "upsert" else None,
            "Timeout": timeout,
            "MemorySize": memory_size,
            "Environment": environment,
            "Runtime": runtime,
            "Layers": layer_arns or None
        })

        function_arn = gen_lambda_arn(function_name, region, account_number)

        add_requirements(context, runtime)

        #Python hack that reduces time to build by 2-3 fold.
        write_requirements_lambda_to_s3(bucket, runtime)
        deploy_requirements_lambda(bucket, runtime, context)
        invoke_requirements_lambda(bucket, object_name)
        check_requirements_built(bucket)
        remove_requirements_lambda(bucket, runtime, context)

        #All other runtimes that require building:
        setup_codebuild_project(bucket, codebuild_project_override_def, runtime, op)
        run_codebuild_build(codebuild_build_override_def)

        #Then we can deploy the lambda
        get_function(prev_state, function_name, desired_config, tags, bucket, object_name, trust_level) #Moved here so that we can do object checks
        create_function(function_name, desired_config, bucket, eh.state.get("new_object_name") or object_name, tags)
        update_function_configuration(function_name, desired_config)
        update_function_code(function_name, bucket, eh.state.get("new_object_name") or object_name)
        remove_tags(function_arn)
        add_tags(function_arn)
        remove_function()
        remove_role(policies, policy_arns, role_description, role_tags)
        gen_props(function_name, region)
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": str(e)}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

def get_default_handler(runtime):
    if runtime.startswith("python") or runtime.startswith("ruby"):
        return "lambda_function.lambda_handler"
    elif runtime.startswith("node"):
        return "index.handler"

def manage_role(op, policies, policy_arns, role_description, role_tags):
    function_arn = lambda_env('role_lambda_name')
    component_def = remove_none_attributes({
        "policies": policies,
        "policy_arns": policy_arns,
        "description": role_description,
        "tags": role_tags
    })

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key="Role", progress_start=0, progress_end=20,
        op=op, merge_props=False)

    return proceed

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
    old_rendef = event.get("prev_state", {}).get("rendef", {})
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)
    
    if old_rendef == new_rendef:
        eh.add_op("compare_etags")

    else:
        eh.add_log("Definitions Don't Match, Deploying", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="compare_etags")
def compare_etags(event, bucket, object_name):
    old_props = event.get("prev_state", {}).get("props", {})

    initial_etag = old_props.get("initial_etag")

    #Get new etag
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        new_etag = eh.state["zip_etag"]
        if initial_etag == new_etag:
            eh.add_log("Full Trust: No Change Detected", {"initial_etag": initial_etag, "new_etag": new_etag})
            eh.add_props(old_props)
            eh.add_links(event.get("prev_state", {}).get("links", {}))
            eh.add_state(event.get("prev_state", {}).get("state", {}))
            eh.declare_return(200, 100, success=True)

        else:
            eh.add_log("Code Changed, Deploying", {"old_etag": initial_etag, "new_etag": new_etag})

@ext(handler=eh, op="load_initial_props")
def load_initial_props(bucket, object_name):
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"): #If not found, retry has already been declared
        eh.add_props({"initial_etag": eh.state.get("zip_etag")})

@ext(handler=eh, op="upsert_role")
def upsert_role(prev_state, policies, policy_arns, role_description, role_tags):
    if eh.state.get("role_arn") and prev_state.get("props", {}).get("Role", {}).get("arn"):
        eh.add_op("remove_role")
        # manage_role("delete", policies, policy_arns, role_description, role_tags)
    elif not eh.state.get("role_arn"):
        proceed = manage_role("upsert", policies, policy_arns, role_description, role_tags)
        if proceed:
            eh.add_state({"role_arn": eh.props.get("Role").get("arn")})
    else:
        return 0

@ext(handler=eh, op="remove_role")
def remove_role(policies, policy_arns, role_description, role_tags):
    if not eh.state.get("role_arn"):
        proceed = manage_role("delete", policies, policy_arns, role_description, role_tags)
    

@ext(handler=eh, op="get_lambda")
def get_function(prev_state, function_name, desired_config, tags, bucket, object_name, trust_level):
    # lambda_client = boto3.client("lambda")

    if prev_state and prev_state.get("props", {}).get("name"):
        old_function_name = prev_state["props"]["name"]
        if old_function_name and (old_function_name != function_name):
            eh.add_op("remove_old", {"name": old_function_name, "create_and_remove": True})
            eh.add_op("create_function")
            return 0

    try:
        function = lambda_client.get_function(
            FunctionName=function_name
        )
        eh.add_log("Got Existing Lambda Function", function)

        current_config = function['Configuration']
        print(f'function config = {current_config}')

        function_arn = function.get("FunctionArn")
        eh.add_props({"arn": function_arn})
        
        if trust_level == "zero":
            #We do the full pull from S3 check
            #Check if we need to actually update the functions code
            deployed_hash = current_config.get("CodeSha256")
            function_bytes = s3.get_object(Bucket=bucket, Key=object_name)["Body"]
            bytes_hash = str(base64.b64encode(hashlib.sha256(function_bytes.read()).digest()))[2:-1]
            print(f"function_bytes_hash = {bytes_hash}")

            if deployed_hash == bytes_hash:
                eh.add_log("No Code Change Detected", {"deployed_hash": deployed_hash, "bytes_hash": bytes_hash})
            else:
                eh.add_op("update_function_code")
        
        elif eh.props["initial_etag"] == prev_state.get("props", {}).get("initial_etag"):
            #We just check the stored SHA
            eh.add_log("Elevated Trust; No Code Change", {"initial_etag": eh.props["initial_etag"]})
            
        else:
            eh.add_op("update_function_code")
            

        for k, v in desired_config.items():
            if k == "Layers":
                continue
            elif v != current_config.get(k):
                eh.add_log("Desired Config Doesn't Match", {"current": current_config, "desired": desired_config})
                eh.add_op("update_function_configuration")

        if not eh.ops.get("update_function_configuration"):
            current_layer_arns = set(map(lambda x: x['Arn'], current_config.get("Layers") or []))
            desired_layer_arns = set(desired_config.get("Layers") or [])
            print(f"current_layer_arns = {current_layer_arns}")
            print(f"desired_layer_arns = {desired_layer_arns}")
            if current_layer_arns != desired_layer_arns:
                eh.add_log("Desired Layers Don't Match", {"current": current_layer_arns, "desired": desired_layer_arns})
                eh.add_op("update_function_configuration")

        current_tags = function.get("Tags") or {}
        if tags != current_tags:
            remove_tags = [k for k in current_tags.keys() if k not in tags]
            add_tags = {k:v for k,v in tags.items() if k not in current_tags.keys()}
            if remove_tags:
                eh.add_op("remove_tags", remove_tags)
            if add_tags:
                eh.add_op("add_tags", add_tags)
                    
        #Store hash?
        # update_function_code = True

    except ClientError as e:
        if e.response['Error']['Code'] in ['ResourceNotFound', 'ResourceNotFoundException']:
            eh.add_op("create_function")
            eh.add_log("Function Does Not Exist", {"name": function_name})
        else:
            handle_common_errors(e, eh, "Get Function Failed", 20)
    

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
                subprocess.call('pip install -r requirements.txt -t .'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            print(os.listdir())

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
def deploy_requirements_lambda(bucket, runtime, context):
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
        arn=context.function_name, component_def=component_def, 
        object_name=eh.state["requirements_object_name"],
        child_key="Requirements Lambda", progress_start=25, progress_end=30,
        op="upsert", ignore_props_links=True
    )

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
            eh.add_log("Requirements Built", response)
        else:
            eh.add_log(f"Requirements Errored", response)
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
def remove_requirements_lambda(bucket, runtime, context):

    component_def = {
        "name": eh.state["requirements_lambda_name"],
        "role_arn": eh.state["this_role_arn"],
        "runtime": runtime,
        "memory_size": 2048,
        "timeout": 60,
        "description": "Temporary Lambda for adding requirements, will be removed",
    }

    eh.invoke_extension(
        arn=context.function_name, component_def=component_def, 
        object_name=eh.state["requirements_object_name"],
        child_key="Requirements Lambda", progress_start=35, progress_end=40,
        op="delete", ignore_props_links=True
    )

    if eh.state.get("requirements_failed"):
        eh.perm_error(f"End ", progress=40)

@ext(handler=eh, op="setup_codebuild_project")
def setup_codebuild_project(bucket, codebuild_def, runtime, op):
    if not eh.state.get("codebuild_object_key"):
        eh.add_state({"codebuild_object_key": f"{random_id()}.zip"})

    runtime_version = LAMBDA_RUNTIME_TO_CODEBUILD_RUNTIME[runtime]
    pre_build_commands, build_commands, post_build_commands, buildspec_artifacts = get_default_buildspec_params(runtime)
    container_image = CODEBUILD_RUNTIME_TO_IMAGE_MAPPING[
        f"{list(runtime_version.keys())[0]}{list(runtime_version.values())[0]}"
    ]

    component_def = {
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
        child_key="Codebuild Project", progress_start=25, 
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
        "project_name": eh.props["Codebuild Project"]["name"]
    }

    component_def.update(codebuild_build_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_build_lambda_name"),
        component_def=component_def, 
        child_key="Codebuild Build", progress_start=30, 
        progress_end=45
    )



@ext(handler=eh, op="create_function")
def create_function(function_name, desired_config, bucket, object_name, tags):
    # lambda_client = boto3.client("lambda")

    def retry_create(message):
        eh.add_log("Potential Race Condition Hit", {"Error": message}, is_error=True)
        eh.retry_error(message, 20)

    try:
        create_params = desired_config
        create_params["Code"] = {
                "S3Bucket": bucket,
                "S3Key": object_name
            }
        create_params['Tags'] = tags

        lambda_response = lambda_client.create_function(**remove_none_attributes(create_params))

        # function_arn = lambda_response.get("FunctionArn")
        eh.add_log("Created Lambda Function", lambda_response)

    except ClientError as e:
        print(str(e))
        if e.response['Error']['Code'] in ['ResourceConflictException', 'ResourceConflict']:
            eh.add_log("Lambda Already Exists", {"function_name": function_name})
            eh.add_op("update_function_code")
            eh.add_op("update_function_configuration")
            if tags:
                eh.add_op("add_tags", tags)
        elif str(e) == "An error occurred (InvalidParameterValueException) when calling the CreateFunction operation: The role defined for the function cannot be assumed by Lambda.":
            #Need to check whether this is actually a race condition or whether it is a role_policy issue
            retry_create(str(e))
        elif str(e).startswith("An error occurred (InvalidParameterValueException) when calling the CreateFunction operation: Lambda was unable to configure access to your environment variables because the KMS key is invalid for CreateGrant. Please check your KMS key settings. KMS Exception: InvalidArnException KMS Message: ARN does not refer to a valid principal:"):
            #Need to check whether this is actually a race condition or whether it is a role_policy issue
            retry_create(str(e))
        else:
            handle_common_errors(e, eh, "Create Function Failed", 40, [
                'InvalidParameterValue', 'CodeStorageExceeded', 'CodeVerificationFailed', 
                'InvalidCodeSignature', 'CodeSigningConfigNotFound'
            ])


@ext(handler=eh, op="update_function_configuration")
def update_function_configuration(function_name, desired_config):
    # lambda_client = boto3.client("lambda")
    print(f"Inside update config, desired_config = {desired_config}")
    try:
        _ = desired_config.pop("Code")
    except:
        pass
    try:
        lambda_response = lambda_client.update_function_configuration(
            **desired_config
        )
        eh.add_log("Updated Function Config", lambda_response)

    except ClientError as e:
        handle_common_errors(e, eh, "Config Update Failed", 60, [
            'PreconditionFailed', 'CodeVerificationFailed', 
            'InvalidCodeSignature', 'CodeSigningConfigNotFound'
            ])


@ext(handler=eh, op="update_function_code")
def update_function_code(function_name, bucket, object_name):
    # lambda_client = boto3.client("lambda")

    try:
        lambda_response = lambda_client.update_function_code(
            FunctionName=function_name,
            S3Bucket=bucket,
            S3Key=object_name
        )
        # function_arn = lambda_response.get("FunctionArn")
        eh.add_log("Updated Function Code", lambda_response)

    except ClientError as e:
        handle_common_errors(e, eh, "Code Update Failed", 75, [
            'InvalidParameterValue', 'CodeStorageExceeded', 
            'PreconditionFailed', 'CodeVerificationFailed', 
            'InvalidCodeSignature', 'CodeSigningConfigNotFound'
            ])
        

@ext(handler=eh, op="gen_props")
def gen_props(function_name, region):
    # lambda_client = boto3.client("lambda")

    try:
        response = lambda_client.get_function(
            FunctionName=function_name
        )
        function = response["Configuration"]
        eh.add_props({
            "name": function['FunctionName'],
            "arn": function['FunctionArn'],
            "code_size": function['CodeSize'],
            "code_sha": function['CodeSha256'],
            "last_modified": function["LastModified"],
            "master_arn": function.get("MasterArn"),
            "layers": list(map(lambda x:x["Arn"], function.get("Layers", [])))
        })
        eh.add_links({
            "Function": gen_lambda_link(function_name, region),
            "Log Group": gen_logs_link(function_name, region)
        })
    except ClientError as e:
        handle_common_errors(e, eh, "Get Props Failed", 98)

@ext(handler=eh, op="add_tags")
def add_tags(function_arn):
    # lambda_client = boto3.client("lambda")
    tags = eh.ops['add_tags']

    try:
        lambda_client.tag_resource(
            Resource=function_arn,
            Tags=tags
        )
        eh.add_log("Tags Added", {"tags": tags})

    except ClientError as e:
        handle_common_errors(e, eh, "Add Tags Failed", 85, ['InvalidParameterValueException'])
        

@ext(handler=eh, op="remove_tags")
def remove_tags(function_arn):
    # lambda_client = boto3.client("lambda")

    try:
        lambda_client.untag_resource(
            Resource=function_arn,
            TagKeys=eh.ops['remove_tags']
        )
        eh.add_log("Tags Removed", {"tags": eh.ops['remove_tags']})

    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Remove Tags Failed", 92, ['InvalidParameterValueException'])


def gen_lambda_arn(function_name, region, account_number):
    #arn:aws:iam::227993477930:policy/3aba481ac88bcbc5d94567e9f93339a7-iam
    return f"arn:aws:lambda:{region}:{account_number}:function:{function_name}"

def gen_lambda_link(function_name, region):
    return f"https://console.aws.amazon.com/lambda/home?region={region}#/functions/{function_name}"

def gen_logs_link(function_name, region):
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/$252Faws$252Flambda$252F{function_name}"

@ext(handler=eh, op="remove_old")
def remove_function():
    # lambda_client = boto3.client("lambda")

    op_def = eh.ops['remove_old']
    function_to_delete = op_def['name']
    create_and_delete = op_def.get("create_and_delete") or False

    try:
        delete_response = lambda_client.delete_function(
            FunctionName=function_to_delete
        )
        eh.add_log(f"Deleted Function", delete_response)

    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            eh.add_log(f"Function Does Not Exist", {"function_name": function_to_delete})
        else:
            eh.retry_error(str(e), 90 if create_and_delete else 15)
            eh.add_log(f"Error Deleting Function", {"name": function_to_delete}, True)

def get_default_buildspec_params(runtime):
    pre_build_commands = []
    build_commands = []
    post_build_commands = []
    buildspec_artifacts = {
        "files": [
            "**/*"
        ],
        "base-directory": "target"
    }
    if runtime.startswith("node"):
        build_commands = [
            "echo 'Installing NPM Dependencies'",
            "npm install --production"
        ]

    return pre_build_commands, build_commands, post_build_commands, buildspec_artifacts
        

LAMBDA_RUNTIME_TO_CODEBUILD_RUNTIME = {
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





# except ClientError as e:
#             handle_common_errors(e, eh, "Download Zipfile Failed", 17)
#         except Exception as e:
#             print(str(e))
#             raise e

#         install_directory = f"{tmpdir}/install/"
#         os.mkdir(install_directory)
#         os.chdir(install_directory)
#         with zipfile.ZipFile(filename, 'r') as archive:
#             archive.extractall()

#         requirements_file = f"{install_directory}requirements.txt"
#         if requirements != "$$file":
#             with open(requirements_file, "w") as f:
#                 f.writelines("%s\n" % i for i in requirements)
        
#         if os.path.exists(requirements_file):
#             # with open(requirements_file, "r") as f:
#             #     lines = list(f.readlines())
#             #     eh.add_log("Requirements Found", {"lines": lines})
            
#             subprocess.call('pip install -r requirements.txt -t .'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
#             eh.add_log("Requirements installed", {"requirements": requirements})
#         else:
#             eh.add_log("No Requirements to Install", {"files": os.listdir()})

#         print(os.listdir())

#         zipfile_name = f"{tmpdir}/file2.zip"
#         create_zip(zipfile_name, install_directory[:-1])

#         try:
#             response = s3.upload_file(zipfile_name, bucket, object_name)
#             eh.add_log("Wrote Requirements to S3", response)
#         except boto3.exceptions.S3UploadFailedError:
#             eh.add_log("Writing Requirements to S3 Failed", {"zipfile_name": zipfile_name, "requirements": requirements})
#             eh.retry_error("S3 Upload Error for Requirements", 25)
#         except ClientError as e:
#             handle_common_errors(e, eh, "Writing Requirements to S3 Failed", 25)
