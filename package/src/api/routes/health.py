from fastapi import APIRouter
import os

router = APIRouter()

@router.get("/health", tags=["health"])
def health_check():
    return {
        "status": "healthy",
        "service": "fintech-serverless-api",
        "version": os.environ.get("APP_VERSION", "1.0.0"),
        "environment": os.environ.get("ENVIRONMENT", "dev"),
        "stack": {
            "runtime": "Python 3.12 + FastAPI + Mangum",
            "compute": "AWS Lambda",
            "database": "DynamoDB (on-demand)",
            "queue": "SQS FIFO",
            "auth": "JWT + Lambda Authorizer",
            "deployment": "CodeDeploy Canary 10%/15min",
            "tracing": "AWS X-Ray",
            "cache": "L1 Lambda Execution Context + L2 DynamoDB"
        },
        "links": {
            "docs": "/dev/docs",
            "github": "github.com/raynercodes/fintech-serverless-api"
        }
    }
