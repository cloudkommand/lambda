import time
import json
import datetime
import re
import base64
import hashlib
import uuid
import os
import boto3
import botocore
import zipfile
import fastjsonschema

from urllib.parse import quote

NAME_REGEX = r"^[a-zA-Z0-9\-\_]+$"
LOWERCASE_NAME_REGEX = r"^[a-z0-9\-\_]+$"
NO_UNDERSCORE_NAME_REGEX = r"^[a-zA-Z0-9\-]+$"
NO_UNDERSCORE_LOWERCASE_NAME_REGEX = r"^[a-z0-9\-]+$"

def safe_encode(string):
    return base64.b32encode(string.encode("ascii")).decode("ascii").replace("=", "8")

def safeval(string, no_underscores, no_uppercase):
    if no_uppercase and no_underscores:
        the_regex=NO_UNDERSCORE_LOWERCASE_NAME_REGEX
    elif no_uppercase:
        the_regex=LOWERCASE_NAME_REGEX
    elif no_underscores:
        the_regex=NO_UNDERSCORE_NAME_REGEX
    else:
        the_regex=NAME_REGEX

    if not re.match(the_regex, string):
        string = safe_encode(string).lower()

    return string
    
def process_repo_id(repo_id, no_underscores, no_uppercase):
    if "<" in repo_id:
        base_repo_id, folder = repo_id.split("<")
    else:
        base_repo_id = repo_id
        folder = None
    repo_provider = None
    if base_repo_id.startswith("github.com/"):
        _, owner_name, repo_name = base_repo_id.split("/")
        repo_provider = "g"
        
    elif base_repo_id.startswith("bitbucket."):
        _, owner_name, repo_name = base_repo_id.split("/")
        repo_provider = "b"

    elif base_repo_id.startswith("gitlab.com/"):
        _, owner_name, repo_name = base_repo_id.split("/")
        repo_provider = "l"

    elif len(base_repo_id.split("/")) == 5:
        # We assume it is a Bitbucket Server Repo
        _, _, owner_name, _, repo_name = base_repo_id.split("/")
        repo_name = repo_name.lower()
        repo_provider = "bs"

    owner_name = safeval(owner_name, no_underscores, no_uppercase)
    repo_name = safeval(repo_name, no_underscores, no_uppercase)
    folder = safeval(folder.replace("/", ""), no_underscores, no_uppercase) if folder else None

    return repo_provider, owner_name, repo_name, folder

def component_safe_name(project_code, repo_id, component_name, no_underscores=False, no_uppercase=False, max_chars=64):
    provider, owner, repo, folder = process_repo_id(repo_id, no_underscores, no_uppercase)
    component_name = safeval(component_name, no_underscores, no_uppercase)

    if folder:
        full_name = f"ck-{project_code}-{provider}-{owner}-{repo}-{folder}-{component_name}"
    else:
        full_name = f"ck-{project_code}-{provider}-{owner}-{repo}-{component_name}"
    
    if len(full_name) > max_chars:
        full_name = f"ck-{hashlib.md5(full_name.encode()).hexdigest()}"
        if len(full_name) > max_chars:
            full_name = full_name[:max_chars]
    return full_name

def remove_none_attributes(payload):
    """Assumes dict"""
    return {k: v for k, v in payload.items() if not v is None}

def random_id():
    return str(uuid.uuid4())

def current_epoch_time_usec_num():
    return int(time.time() * 1000000)

def lambda_env(key):
    try:
        return os.environ.get(key)
    except Exception as e:
        raise e

def create_zip(file_name, path):
    ziph=zipfile.ZipFile(file_name, 'w', zipfile.ZIP_DEFLATED)
    # ziph is zipfile handle
    for root, dirs, files in os.walk(path):
        for file in files:
            ziph.write(os.path.join(root, file), 
                       os.path.relpath(os.path.join(root, file), 
                                       os.path.join(path, '')))
    ziph.close()

def account_context(context):
    vals = context.invoked_function_arn.split(':')
    return {
        "number": vals[4],
        "region": vals[3]
    }

def gen_log(title, details, is_error=False, link=None):
    return {
        "title": title,
        "details": details,
        "timestamp_usec": current_epoch_time_usec_num(),
        "is_error": is_error,
        "link": link
    }

def defaultconverter(o):
    if isinstance(o, datetime.datetime):
        return o.__str__()

def creturn(status_code, progress, success=None, error=None, logs=None, pass_back_data=None, state=None, props=None, links=None, callback_sec=2, error_details={}):
    
    assembled = remove_none_attributes({
        "statusCode": 200,
        "progress": progress,
        "success": success,
        "error": error,
        "error_details": error_details,
        "pass_back_data": pass_back_data,
        "state": state,
        "props": props,
        "links": links,
        "logs": logs,
        "callback_sec":callback_sec
    })
    print(f'assembled = {assembled}')

    return json.loads(json.dumps(assembled, default=defaultconverter))

def handle_common_errors(error, extension_handler, text, progress, perm_errors=[]):
    if error.response['Error']['Code'] in perm_errors:
        extension_handler.add_log(f"{text}: {error.response['Error']['Code']}", {"error": str(error)}, True)
        extension_handler.perm_error(f"{text}: {str(error)}", progress)
        print(f"Permanent Error: {text}: {str(error)}")
    else:
        extension_handler.add_log(f"{text}: {error.response['Error']['Code']}", {"error": str(error)}, True)
        extension_handler.retry_error(f"{text}: {str(error)}", progress)
        print(f"Retry Error: {text}: {str(error)}")


# def sort_f(td):
#     return td['timestamp_usec']

class ExtensionHandler:

    def refresh(self):
        self.logs = []
        self.ops = {}
        self.retries = {}
        self.ret = False
        self.callback_sec = 0
        self.status_code = None
        self.progress = None
        self.success = None
        self.error = None
        self.props = {}
        self.links = {}
        self.state = {}
        self.callback = None
        self.error_details = None
        self.children = {}
        self.op = None
        self.project_code = None
        self.repo_id = None
        self.bucket = None
        self.component_name = None
    
    def __init__(self, ignore_undeclared_return=True, max_retries_per_error_code=6):
        self.refresh()
        self.ignore_undelared_return = ignore_undeclared_return
        self.max_retries_per_error_code = max_retries_per_error_code

    def capture_event(self, event):
        self.refresh()
        if event.get("pass_back_data"):
            self.declare_pass_back_data(event["pass_back_data"])
        self.project_code = event.get("project_code")
        self.repo_id = event.get("repo_id")
        self.bucket = event.get("bucket")
        self.component_name = event.get("component_name")
        self.op = event.get("op")
        
    def declare_pass_back_data(self, pass_back_data):
        pbd = pass_back_data.copy()
        self.ops = pbd.pop('ops', {}) or {}
        self.retries = pbd.pop('retries', {}) or {}
        self.props = pbd.pop("props", {}) or {}
        self.links = pbd.pop("links", {}) or {}
        self.state = pbd.pop("state", {}) or {}
        print(f"Ops = {self.ops}, Retries = {self.retries}, Links = {self.links}, Props = {self.props}")
        self.children = pbd
        if pbd:
            print(f"Set Children {self.children}")

    def invoke_extension(self, arn, component_def, child_key, 
            progress_start, progress_end, object_name=None, 
            op=None, merge_props=False, links_prefix=None,
            ignore_props_links=False, synchronous=True):

        schema = {
            "type": "object",
            "properties": {
                "arn": {"type": "string"},
                "component_def": {"type": "object"},
                "child_key": {"type": "string"},
                "progress_start": {"type": "number"},
                "progress_end": {"type": "number"},
                "object_name": {"type": ["string", "null"]},
                "op": {"type": ["string", "null"]},
                "merge_props": {"type": ["boolean", "null"]},
                "links_prefix": {"type": ["string", "null"]},
                "ignore_props_links": {"type": ["boolean", "null"]},
                "synchronous": {"type": ["boolean", "null"]}
            },
            "required": ["arn", "component_def", "child_key", "progress_start", "progress_end"]
        }

        try:
            fastjsonschema.validate(schema, {
                "arn": arn,
                "component_def": component_def,
                "child_key": child_key,
                "progress_start": progress_start,
                "progress_end": progress_end,
                "object_name": object_name,
                "op": op,
                "merge_props": merge_props,
                "links_prefix": links_prefix,
                "ignore_props_links": ignore_props_links,
                "synchronous": synchronous
            })
        except:
            raise Exception("Invalid invoke_extension parameters")

        if merge_props:
            raise Exception("Cannot Merge Props")
        if child_key in ['ops', 'retries', 'props', 'links']:
            raise Exception(f"Child key cannot be set to {child_key}. Please choose another key")
        
        l_client = boto3.client("lambda")
        child_key = child_key or arn
        op = op or self.op
        
        payload = bytes(json.dumps(remove_none_attributes({
            "component_def": component_def,
            "component_name": self.component_name,
            "op": op,
            "s3_object_name": object_name,
            "pass_back_data": self.children.get(child_key),
            "prev_state": {"props": self.props.get(child_key)} if self.props.get(child_key) else None,
            "bucket": self.bucket,
            "repo_id": self.repo_id,
            "project_code": self.project_code
        })), "utf-8")

        ################################
        # NEED TIMER ON THIS
        ################################

        try:
            response = l_client.invoke(
                FunctionName=arn,
                InvocationType="RequestResponse" if synchronous else "Event",
                LogType="None",
                Payload=payload
            )

            if response.get("StatusCode") not in [200,202,204]:
                print(f'Error = {response["Payload"].read()}')
                raise Exception(f'Function Error = {response.get("FunctionError")}')

            if synchronous:
                result = json.loads(response["Payload"].read())
                print(f"Invoke Result = {result}")

                logs = result.get("logs") or []
                progress = result.get("progress") or 0
                success = result.get("success")
                error = result.get("error")
                props = result.get("props") or {}
                # state = result.get("state") Not Handling child state ATM
                links = result.get("links") or {}
                
                true_progress = int(progress_start + (progress/100 * (progress_end - progress_start)))
                self.logs.extend(logs)
                if error:
                    self.perm_error(error, true_progress)
                    return False
                else:
                    if op == "upsert":
                        if not ignore_props_links:
                            if links_prefix:
                                links = {f"{links_prefix} {k}":v for k,v in links.items()} 
                            self.links.update(links)
                            if props:
                                if isinstance(self.props.get(child_key), dict):
                                    self.props[child_key].update(props)
                                else:
                                    self.props[child_key] = props
                                if merge_props:
                                    self.props.update(props)

                    if not success:
                        pass_back_data = result.get("pass_back_data") or {}
                        self.children[child_key] = pass_back_data
                        self.retry_error(f'{child_key} {pass_back_data.get("last_retry")}', true_progress, callback_sec=result['callback_sec'])
                        proceed=False

                    else:
                        if child_key in self.children:
                            del self.children[child_key]
                        proceed= True
            else:
                proceed=True

        except botocore.exceptions.ClientError as e:
            proceed=False
            if e.response['Error']['Code'] in ["ResourceNotFoundException", "InvalidRequestContentException", "RequestTooLargeException"]:
                self.add_log(f"Error Invoking {child_key}", {"error": str(e)}, True)
                self.add_log(e.response['Error']['Code'], {"error": str(e)}, True)
                self.perm_error(str(e), progress_start)
            else:
                self.add_log(f"Error Invoking {child_key}", {"error": str(e)}, True)
                self.add_log(e.response['Error']['Code'], {"error": str(e)}, True)
                self.retry_error(str(e), progress_start)
        
        return proceed

        
    def add_op(self, opkey, opvalue=True):
        print(f'add op {opkey} with value {opvalue}')
        self.ops[opkey] = opvalue
        
    def complete_op(self, opkey):
        print(f'completing op {opkey}')
        try:
            _ = self.ops.pop(opkey)
        except:
            pass

    def add_props(self, props):
        #Restrict prop key names to alphanumeric, underscore, and hyphen
        # invalid_prop_keys = list(filter(lambda x: (not re.match(NAME_REGEX, x)), props.keys()))
        # if invalid_prop_keys:
        #     raise Exception(f"Invalid Prop Key Names = {invalid_prop_keys}")
        self.props.update(props)
        return self.props

    def add_state(self, state):
        self.state.update(state)
        return self.state

    def add_links(self, links):
        self.links.update(links)
        return self.links
        
    def add_log(self, title, details={}, is_error=False):
        link = gen_log_link()
        print(f"{title}: {details}")
        self.logs.append(gen_log(title, details, is_error, link))

    def perm_error(self, error, progress=0):
        return self.declare_return(200, progress, error_code=error, callback=False)

    def retry_error(self, error, progress=0, callback_sec=0):
        return self.declare_return(200, progress, error_code=error, callback_sec=callback_sec)

    def declare_return(self, status_code, progress, success=None, props=None, links=None, error_code=None, error_details=None, callback=True, callback_sec=0):
        print(f"Calling back to CK, success = {success}, error_code = {error_code}")
        self.status_code = status_code
        self.progress = progress
        self.success = success
        self.error = error_code
        self.props.update(props or {})
        self.links.update(links or {})
        self.callback = callback
        self.callback_sec = callback_sec
        self.error_details = error_details
        self.ret = True
        
    def finish(self):
        pass_back_data = {}
        if self.error:
            pass_back_data['ops'] = self.ops
            pass_back_data['retries'] = self.retries
            this_retries = pass_back_data['retries'].get(self.error, 0) + 1
            pass_back_data['retries'][self.error] = this_retries
            pass_back_data['props'] = self.props
            pass_back_data['links'] = self.links
            pass_back_data['state'] = self.state
            if self.children:
                pass_back_data.update(self.children)
            if this_retries < self.max_retries_per_error_code and self.callback:
                pass_back_data['last_retry'] = self.error
                self.error = None
                self.error_details = None
                if not self.callback_sec:
                    self.callback_sec = 2**this_retries                

        elif not self.success and not self.ignore_undelared_return:
            self.error = "no_success_or_error"
            self.error_details = {"error": "Finish was called without either success or an error code being passed."}

        elif not self.success:
            self.success=True
            self.progress=100

#       self.logs.sort(key=sort_f, reverse=True)
            
        return creturn(
            self.status_code, self.progress, self.success, self.error, self.logs, 
            pass_back_data, self.state or None, self.props, self.links, self.callback_sec, self.error_details
        )
    
# A decorator
def ext(f=None, handler=None, op=None, complete_op=True):
    import functools
    
    if not f:
        return functools.partial(
            ext,
            handler=handler,
            op=op,
            complete_op=complete_op
        )

    if not handler:
        raise Exception(f"Must pass handler of type ExtensionHandler to ext decorator")

    @functools.wraps(f)
    def the_wrapper_around_the_original_function(*args, **kwargs):
        try:
            if handler.ret:
                return None
            elif op and op not in handler.ops.keys():
                # prin(f"Not trying function {f.__name__}, not in ops")
                return None
        except:
            raise Exception(f"Must pass handler of type ExtensionHandler to ext decorator")

        result = f(*args, **kwargs)
        if complete_op and not handler.ret:
            handler.complete_op(op)
        return result

    return the_wrapper_around_the_original_function

def gen_log_link():
    log_event_encoded = quote(quote(lambda_env("AWS_LAMBDA_LOG_STREAM_NAME"), safe=''), safe='').replace("%", "$")
    region = lambda_env("AWS_DEFAULT_REGION")
    # Get milliseconds since epoch
    millis = int(round(time.time() * 1000)) - 50
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/$252Faws$252Flambda$252F{os.environ['AWS_LAMBDA_FUNCTION_NAME']}/log-events/{log_event_encoded}$3Fstart$3D{millis}"
