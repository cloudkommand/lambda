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
        cdef = event.get("component_def")
        s3_key = event.get("s3_object_name")
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
            
        assembled = {
            "statusCode": 200,
            "progress": 100,
            "success": True,
            "logs": [{
                "title": "Success Adding Requirements",
                "details": {"dependencies": requirements},
                "timestamp_usec": time.time() * 1000000,
                "is_error": False
            }],
        }
        print(assembled)
        return json.loads(json.dumps(assembled, default=defaultconverter))

    except Exception as e:
        assembled = {
            "statusCode": 200,
            "progress": 10,
            "error": str(e),
            "logs": [{
                "title": "Error Adding Requirements",
                "details": {"error": str(e)},
                "timestamp_usec": time.time() * 1000000,
                "is_error": True
            }],
            "callback_sec":2
        }
        print(assembled)
        return json.loads(json.dumps(assembled, default=defaultconverter))
        
        
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