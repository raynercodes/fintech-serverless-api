import boto3
import os
import json


def handler(event, context):
    codedeploy = boto3.client("codedeploy", region_name="us-east-1")
    deployment_id = event["DeploymentId"]
    lifecycle_event_hook_id = event["LifecycleEventHookExecutionId"]

    try:
        # Check CloudWatch for errors on the new Lambda version
        # during the 10% canary window
        cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")

        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Errors",
            Dimensions=[
                {
                    "Name": "FunctionName",
                    "Value": os.environ["FUNCTION_NAME"]
                }
            ],
            StartTime=context.invoked_function_arn,
            # 900 seconds = 15 minutes
            Period=900,
            Statistics=["Sum"]
        )

        datapoints = response.get("Datapoints", [])
        error_count = sum(dp["Sum"] for dp in datapoints)

        if error_count > 0:
            raise Exception(
                f"Errors detected during canary window: {error_count} errors"
            )

        # No errors detected — confirm full traffic shift
        print("AfterAllowTraffic validation passed — no errors in canary window")
        codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status="Succeeded"
        )

    except Exception as e:
        # Errors detected in canary window — roll back immediately
        # Remaining 90% never shifts to the new version
        print(f"AfterAllowTraffic validation failed: {str(e)}")
        codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status="Failed"
        )
