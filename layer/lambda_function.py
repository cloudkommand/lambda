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
    handle_common_errors

eh = ExtensionHandler()

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
        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")

        pass_back_data = event.get("pass_back_data", {})
        if pass_back_data:
            pass
        elif event.get("op") == "upsert":
            if prev_state and prev_state.get("props") and (prev_state.get("props").get("name")) != layer_name:
                eh.add_op("remove_layer_versions", {"name":prev_state.get("props").get("name")})
                eh.add_op("publish_layer_version")
                if cdef.get("requirements"):
                    eh.add_op("add_requirements", cdef.get("requirements"))
                elif cdef.get("requirements.txt"):
                    eh.add_op("add_requirements", "$$file")
            elif prev_state:
                eh.add_op("check_if_update_required")
                if cdef.get("requirements"):
                    eh.add_op("add_requirements", cdef.get("requirements"))
                elif cdef.get("requirements.txt"):
                    eh.add_op("add_requirements", "$$file")
            else:
                eh.add_op("publish_layer_version")

        elif event.get("op") == "delete":
            eh.add_op("remove_layer_versions", {"name": layer_name})

        add_requirements(bucket, object_name)
        check_if_update_required(prev_state, bucket, object_name)
        publish_layer_version(layer_name, cdef, bucket, object_name, region)
        remove_layer_versions(event.get("op"))

        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Uncovered Error", {"error": msg}, is_error=True)
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
def add_requirements(bucket, object_name):
    requirements = eh.ops["add_requirements"]

    s3 = boto3.client("s3")
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            data = s3.get_object(Bucket=bucket, Key=object_name)["Body"]
            filename = f"{tmpdir}/file.zip"
            with open(filename, "wb") as f:
                f.write(data.read())
        except ClientError as e:
            handle_common_errors(e, eh, "Download Zipfile Failed", 17)
        except Exception as e:
            print(str(e))
            raise e

        install_directory = f"{tmpdir}/install/"
        os.mkdir(install_directory)
        os.chdir(install_directory)
        with zipfile.ZipFile(filename, 'r') as archive:
            archive.extractall()

        requirements_file = f"{install_directory}requirements.txt"
        if requirements != "$$file":
            with open(requirements_file, "w") as f:
                f.writelines("%s\n" % i for i in requirements)
        
        if os.path.exists(requirements_file):
            dirs = os.listdir()
            print(dirs)
            #We are going to assume there is only one directory, as there should be
            
            subprocess.call(f'pip install -r requirements.txt -t ./{dirs[0]}'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            eh.add_log("Requirements installed", {"requirements": requirements})
        else:
            eh.add_log("No Requirements to Install", {"files": os.listdir()})

        zipfile_name = f"{tmpdir}/file2.zip"
        create_zip(zipfile_name, install_directory[:-1])

        try:
            response = s3.upload_file(zipfile_name, bucket, object_name)
            eh.add_log("Wrote Requirements to S3", response)
        except boto3.exceptions.S3UploadFailedError:
            eh.add_log("Writing Requirements to S3 Failed", {"zipfile_name": zipfile_name, "requirements": requirements})
            eh.retry_error("S3 Upload Error for Requirements", 25)
        except ClientError as e:
            handle_common_errors(e, eh, "Writing Requirements to S3 Failed", 25)


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
            eh.add_log("Error in Getting Layer Version", {'layer_name': props.get("name")})
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