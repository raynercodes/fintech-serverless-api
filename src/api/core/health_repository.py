import os


def get_health_status() -> dict:
    """
    Pulled into core/ anyway to match the thin-route convention used
    everywhere else, even though (unlike loans/auth) there's no real
    I/O being separated out here — just keeping routes consistently
    thin across the whole project.
    """
    return {
        "status": "healthy",
        "service": "Fintech Serverless Loan Lending Platform",
        "version": os.environ.get("APP_VERSION", "1.0.0"),
        "environment": os.environ.get("ENVIRONMENT", "dev"),
        "author": {
            "name": "Leonardo Rayner",
            "github": "github.com/raynercodes",
            "portfolio": "raynercodes.dev",
            "linkedin": "linkedin.com/in/leonardo-rayner-raynercodes/"
        },
        "stack": {
            "runtime": "Python 3.12 + FastAPI + Mangum",
            "compute": "AWS Lambda (5 functions)",
            "database": "DynamoDB on-demand — composite sort key, 2 GSIs, zero table scans",
            "queue": "SQS FIFO — two-layer duplicate prevention with conditional writes",
            "auth": "JWT + Lambda Authorizer + brute force progressive lockout",
            "encryption": "AES-256-GCM on sensitive PII — separate KMS key per secret category",
            "caching": "L0 CloudFront + L1 Lambda execution context + L2 DynamoDB cache table",
            "deployment": "CodeDeploy canary 10%/15min — automated rollback via CloudWatch alarms",
            "pipeline": "GitHub Actions CI → CodePipeline CD — two manual approval gates",
            "iac": "AWS SAM + CloudFormation — single source of truth",
            "observability": "X-Ray tracing + CloudWatch structured logging",
            "maintenance": "EventBridge CRON — automated cleanup and backups"
        },
        "links": {
            "interactive docs": "https://pw4kfpuw3f.execute-api.us-east-1.amazonaws.com/dev/docs",
            "github": "github.com/raynercodes/fintech-serverless-api",
            "portfolio": "raynercodes.dev"
        },
        "instructions": {
            "step_1": "GET /dev/health — you are here",
            "step_2": "copy the link below and paste it into your browser to access the interactive docs to test endpoints in a sandbox UI",
            "docs": "https://pw4kfpuw3f.execute-api.us-east-1.amazonaws.com/dev/docs",
            "Disclaimer": "Docs have examples and instructions for each endpoint — always refer back to the Steps at the top of the page when stuck — Thanks for testing!"
        }
    }