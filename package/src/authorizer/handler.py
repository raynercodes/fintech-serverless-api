import json
from src.api.core.security import verify_jwt


def generate_policy(principal_id: str, effect: str, resource: str, context: dict = None) -> dict:
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }
    if context:
        policy["context"] = context
    return policy


def handler(event, context):
    try:
        # API Gateway passes the token in the Authorization header
        # Format: "Bearer eyJhbGciOiJIUzI1NiJ9..."
        auth_header = event.get("authorizationToken", "")

        if not auth_header.startswith("Bearer "):
            return generate_policy("unauthorized", "Deny", event["methodArn"])

        token = auth_header.split(" ")[1]

        # verify_jwt returns None if token is invalid or expired
        payload = verify_jwt(token)

        if payload is None:
            return generate_policy("unauthorized", "Deny", event["methodArn"])

        # Token is valid — extract identity from payload
        account_id = payload.get("account_id", "unknown")
        customer_id = payload.get("customer_id", "unknown")

        # Pass identity context to main Lambda function
        # FastAPI can access these via event["requestContext"]["authorizer"]
        context_data = {
            "account_id": account_id,
            "customer_id": customer_id
        }

        return generate_policy(
            account_id,
            "Allow",
            event["methodArn"],
            context_data
        )

    except Exception:
        return generate_policy("unauthorized", "Deny", event["methodArn"])
