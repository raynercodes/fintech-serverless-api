import boto3
import os
import json
from datetime import datetime, timezone, timedelta


def handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    codedeploy = boto3.client("codedeploy", region_name="us-east-1")
    deployment_id = event.get("DeploymentId")
    lifecycle_event_hook_id = event.get("LifecycleEventHookExecutionId")
    status = "Succeeded"

    try:
        # Check CloudWatch for errors on the new Lambda version
        # during the 10% canary window
        cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=15)

        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Errors",
            Dimensions=[
                {
                    "Name": "FunctionName",
                    "Value": os.environ["FUNCTION_NAME"]
                }
            ],
            StartTime=start_time,
            EndTime=end_time,
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

    except Exception as e:
        # Errors detected in canary window — roll back immediately
        # Remaining 90% never shifts to the new version
        print(f"AfterAllowTraffic validation failed: {str(e)}")
        codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status="Failed"
        )
    try:
        response = codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status=status
        )
        print(f"CodeDeploy callback succeeded with status: {status}")
        print(f"Response: {response}")
    except Exception as e:
        print(f"CodeDeploy callback FAILED — this is a permissions issue: {str(e)}")
        raise

    return {"status": status}
