import boto3
import os
import json


def handler(event, context):
    codedeploy = boto3.client("codedeploy", region_name="us-east-1")
    deployment_id = event["DeploymentId"]
    lifecycle_event_hook_id = event["LifecycleEventHookExecutionId"]

    try:
        # Verify DynamoDB tables are accessible before allowing traffic
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        transactions_table = dynamodb.Table(
            os.environ["DYNAMODB_TABLE_NAME"]
        )
        cache_table = dynamodb.Table(
            os.environ["CACHE_TABLE_NAME"]
        )

        # Simple read to confirm tables are reachable
        # If DynamoDB is down I catch it here before any traffic shifts
        transactions_table.load()
        cache_table.load()

        # Verify SQS queue is accessible
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.get_queue_attributes(
            QueueUrl=os.environ["SQS_QUEUE_URL"],
            AttributeNames=["QueueArn"]
        )

        # Verify Secrets Manager is accessible
        secrets = boto3.client("secretsmanager", region_name="us-east-1")
        secrets.describe_secret(
            SecretId="/fintech/prod/jwt-secret"
        )

        # All checks passed — allow traffic to shift
        print("BeforeAllowTraffic validation passed — all services reachable")
        codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status="Succeeded"
        )

    except Exception as e:
        # Something is wrong — tell CodeDeploy to roll back
        # New version never receives any traffic
        print(f"BeforeAllowTraffic validation failed: {str(e)}")
        codedeploy.put_lifecycle_event_hook_execution_status(
            deploymentId=deployment_id,
            lifecycleEventHookExecutionId=lifecycle_event_hook_id,
            status="Failed"
        )
