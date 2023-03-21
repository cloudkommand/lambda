import boto3
import botocore
# import jsonschema
import json
import traceback

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, \
    ext, component_safe_name, handle_common_errors

# def validate_state(state):
# "prev_state": prev_state,
# "component_def": component_def, RENDERED
# "op": op,
# "s3_object_name": object_name,
# "pass_back_data": pass_back_data
#     jsonschema.validate()
eh = ExtensionHandler()

l_client = boto3.client("lambda")
def lambda_handler(event, context):
    try:
        print(f"event = {event}")
        # account_number = account_context(context)['number']
        eh.capture_event(event)

        prev_state = event.get("prev_state")
        cdef = event.get("component_def")
        # cname = event.get("component_name")
        # project_code = event.get("project_code")
        # repo_id = event.get("repo_id")
        event_source_arn = cdef.get("event_source_arn")
        function_arn = cdef.get("function_arn")
        if not event_source_arn or not function_arn:
            raise Exception("event_source_arn and function_arn must be defined")
        
        enabled = cdef.get("enabled") if (cdef.get("enabled") is not None) else True
        batch_size = cdef.get("batch_size") or 10
        filter_criteria = cdef.get("filter_criteria")
        maximum_batching_window = cdef.get("maximum_batching_window")
        parallelization_factor = cdef.get("parallelization_factor")
        starting_position_timestamp = cdef.get("starting_position_timestamp")
        starting_position = cdef.get("starting_position") if not starting_position_timestamp else "AT_TIMESTAMP"
        on_success_arn = cdef.get("on_success_arn")
        on_failure_arn = cdef.get("on_failure_arn")
        maximum_record_age = cdef.get("maximum_record_age")
        bisect_batch_on_error = cdef.get("bisect_batch_on_error")
        maximum_retry_attempts = cdef.get("maximum_retry_attempts")
        tumbling_window = cdef.get("tumbling_window")
        # kafka_topic = [cdef.get("kafka_topic")] if cdef.get("kafka_topic") else None
        # mq_queue = [cdef.get("mq_queue")] if cdef.get("mq_queue") else None
        function_response_types = cdef.get("function_response_types")        
        
        pass_back_data = event.get("pass_back_data", {})
        if pass_back_data:
            pass
        elif event.get("op") == "upsert":
            eh.add_op("get_event_mapping")

        elif event.get("op") == "delete":
            eh.add_op("delete_event_mapping", {"uuid":prev_state["props"]["uuid"]})

        else:
            raise Exception("op must be upsert or delete")

        configuration = remove_none_attributes({
            "FunctionName": function_arn,
            "EventSourceArn": event_source_arn,
            "Enabled": enabled,
            "BatchSize": batch_size,
            "FilterCriteria": filter_criteria,
            "MaximumBatchingWindowInSeconds": maximum_batching_window,
            "StartingPosition": starting_position,
            "StartingPositionTimestamp": starting_position_timestamp,
            "ParallizationFactor": parallelization_factor,
            "DestinationConfig": remove_none_attributes({
                "OnSuccess": remove_none_attributes({
                    "Destination": on_success_arn
                }) or None,
                "OnFailure": remove_none_attributes({
                    "Destination": on_failure_arn
                }) or None
            }) or None,
            "MaximumRecordAgeInSeconds": maximum_record_age,
            "BisectBatchOnFunctionError": bisect_batch_on_error,
            "MaximumRetryAttempts": maximum_retry_attempts,
            "TumblingWindowInSeconds": tumbling_window,
            "FunctionResponseTypes": function_response_types
        })
        print(f"configuration = {configuration}")

        get_event_mapping(prev_state, event_source_arn, function_arn, configuration)
        create_event_mapping(event_source_arn, function_arn, configuration)
        update_event_mapping(configuration)
        delete_event_mapping()

        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

@ext(handler=eh, op="get_event_mapping")
def get_event_mapping(prev_state, event_source_arn, function_name, configuration):
    # create_and_remove = False

    try:
        response = l_client.list_event_source_mappings(
            EventSourceArn=event_source_arn,
            MaxItems=100
        )

        current_configurations = list(filter(lambda x: x["FunctionArn"] == function_name, response.get("EventSourceMappings") or []))
        if not len(current_configurations):
            eh.add_op("create_event_mapping")
            if prev_state and prev_state.get("props", {}).get("uuid"):
                eh.add_op("delete_event_mapping", {"uuid":prev_state["props"]["uuid"], "create_and_remove": True})
        else:
            current_configuration = current_configurations[0]
            eh.add_props({
                "uuid": current_configuration['UUID'],
                "function_arn": current_configuration['FunctionArn'],
                "event_source_arn": current_configuration['EventSourceArn'],
            })

            for k,v in configuration.items():
                if current_configuration.get(k) != v:
                    if k == "FunctionName" and (current_configuration.get("FunctionArn") == v):
                        continue
                    elif k == "Enabled" and (((v == True) and (current_configuration.get("State") == "Enabled")) or ((v == False) and (current_configuration.get("State") == "Disabled"))):
                        continue
                    eh.add_log("Mapping Configuration Changed", {"configuration": configuration, "current_configuration": current_configuration})
                    eh.add_op("update_event_mapping")
                    break
            
            if not eh.ops.get("update_event_mapping"):
                eh.add_log("Mapping In Place. Exiting", {"configuration": configuration, "current_configuration": current_configuration})
         
    except ClientError as e:
        handle_common_errors(e, eh, "Error Listing Event Mappings", 0)


@ext(handler=eh, op="create_event_mapping")
def create_event_mapping(event_source_arn, function_name, configuration):
    try:
        role_response = l_client.create_event_source_mapping(**configuration)
        eh.add_props({
            "uuid": role_response['UUID'],
            "function_arn": role_response['FunctionArn'],
            "event_source_arn": role_response['EventSourceArn'],
        })
        eh.add_log("Created New Event Mapping", role_response)

    except botocore.exceptions.ClientError as e:
        if "An error occurred (InvalidParameterValueException) when calling the CreateEventSourceMapping operation: The provided execution role does not have permissions to call" in str(e):
            eh.add_log("Lambda Needs Additional SQS Permissions", {"function_arn": function_name}, is_error=True)
            eh.perm_error(str(e), 20)
        elif e.response['Error']['Code'] == 'ResourceConflictException':
            pass #Recreate?
        else:
            handle_common_errors(e, eh, "Error Creating Mapping", 20, ["InvalidParameterValueException", "ResourceNotFoundException"])
    

@ext(handler=eh, op="update_event_mapping")
def update_event_mapping(configuration):
    uuid = eh.props["uuid"]

    configuration = {k:v for k,v in configuration.items() if k not in [
        "EventSourceArn", "StartingPosition", "StartingPositionTimestamp"
    ]}
    configuration["UUID"] = uuid

    try:
        response = l_client.update_event_source_mapping(**configuration)
        eh.add_log("Updated Event Source Mapping", response)

    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Error Updating Mapping", 30, ["InvalidParameterValueException", "ResourceNotFoundException"])

@ext(handler=eh, op="delete_event_mapping")
def delete_event_mapping():
    uuid = eh.ops['delete_event_mapping'].get("uuid")
    car = eh.ops['delete_event_mapping'].get("create_and_remove")

    try:
        delete_response = l_client.delete_event_source_mapping(
            UUID=uuid
        )
        eh.add_log("Event Mapping Deleted", delete_response)

    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            eh.add_log("No Event Mapping Found", {"uuid": uuid})
        else:
            handle_common_errors(e, eh, "Error Deleting Mapping", 80 if car else 0)
            
    

