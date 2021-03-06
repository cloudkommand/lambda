import boto3
import botocore
from botocore.exceptions import ClientError

import json
import os
import subprocess
import tempfile
import traceback
import zipfile

ALLOWED_RUNTIMES = ["python3.9", "python3.8", "python3.6", "python3.7", "nodejs14.x", "nodejs12.x", "nodejs10.x", "ruby2.7", "ruby2.5"]


from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, create_zip, \
    handle_common_errors, random_id, lambda_env

eh = ExtensionHandler()

lambda_client = boto3.client("lambda")
s3 = boto3.client("s3")
def lambda_handler(event, context):
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
        layer_name = cdef.get("name") or component_safe_name(project_code, repo_id, cname)
        runtime = cdef.get("requirements_runtime") or "python3.9"
        # if requirements_runtime and requirements_runtime not in ALLOWED_RUNTIMES:
        #     eh.perm_error("requirements_runtime invalid", {"runtime": requirements_runtime, "allowed": ALLOWED_RUNTIMES})
        
        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")

        pass_back_data = event.get("pass_back_data", {})
        if pass_back_data:
            pass
        elif event.get("op") == "upsert":
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
            eh.add_op("remove_layer_versions", {"name": layer_name})

        add_requirements()
        write_requirements_lambda_to_s3(bucket, runtime)
        deploy_requirements_lambda(bucket, runtime)
        invoke_requirements_lambda(bucket, object_name)
        check_requirements_built(bucket)
        remove_requirements_lambda(bucket, runtime)
        check_if_update_required(prev_state, bucket, object_name)
        publish_layer_version(layer_name, cdef, bucket, object_name, region)
        remove_layer_versions(event.get("op"))

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
        eh.add_log("Cound Not Find Zip", {"bucket": bucket, "key": object_name})
        eh.retry_error("Object Not Found")

@ext(handler=eh, op="add_requirements")
def add_requirements():
    try:
        response = lambda_client.get_function(
            FunctionName=lambda_env("function_lambda_name")
        )
        role_arn = response["Configuration"]["Role"]
        eh.add_state({"this_role_arn": role_arn})
        eh.add_op("write_requirements_lambda_to_s3")
        
    except ClientError as e:
        handle_common_errors(e, eh, "Get Requirements Role Failed: ", 25)

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
                dirs = next(os.walk('.'))[1]
                print(dirs)
                #We are going to assume there is only one directory, as there should be
                
                subprocess.call(f'pip install -r requirements.txt -t ./{dirs[0]}'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)                

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
        eh.perm_error(f"End ", progress=40)


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
            else:
                eh.add_log("New Layer Version Required", {"etag": eh.state.get('zip_etag'), "code_etag": code_etag})
                eh.add_op("publish_layer_version")
        else:
            return None


@ext(handler=eh, op="publish_layer_version")
def publish_layer_version(layer_name, cdef, bucket, object_name, region):
    lambda_client = boto3.client("lambda")
    if not eh.state.get("zip_etag"):
        get_s3_etag(bucket, object_name)
    
    description = eh.state.get("zip_etag")
    compatible_runtimes = cdef.get("compatible_runtimes") or ["python3.9", "python3.8", "python3.6", "python3.7"]    
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

def gen_layer_link(layer_name, region):
    return f"https://console.aws.amazon.com/lambda/home?region={region}#/layers/{layer_name}"