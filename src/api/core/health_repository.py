import os
import json
from fastapi import Request

def _load_version() -> str:
    version_path = os.path.join(os.path.dirname(__file__), "..", "version.json")
    try:
        with open(version_path) as f:
            return json.load(f)["version"]
    except Exception:
        return "1.0.0"

def get_health_status(request: Request = None) -> dict:
    """
    Pulled into core/ anyway to match the thin-route convention used
    everywhere else, even though (unlike loans/auth) there's no real
    I/O being separated out here — just keeping routes consistently
    thin across the whole project.
    """
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
    if ENVIRONMENT == "prod":
        BASE_URL = os.environ.get("PROD_CUSTOM_DOMAIN", "").rstrip("/")
    elif request is not None:
        # Mangum + FastAPI's root_path already correctly reflects
        # whatever domain/stage the request ACTUALLY came in on —
        # no CloudFormation reference needed, always accurate, and
        # self-updating even if the API ID ever changes again
        BASE_URL = str(request.base_url).rstrip("/")
    else:
        BASE_URL = "" # fallback for local dev/testing, no request context available
    VERSION = _load_version()
    
    return {
        "status": "healthy",
        "service": "Fintech Serverless Loan Lending Platform",
        "version": VERSION,
        "environment": ENVIRONMENT,
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
            "interactive docs": f"{BASE_URL}/docs",
            "github": "github.com/raynercodes/fintech-serverless-api",
            "portfolio": "raynercodes.dev"
        },
        "instructions": {
            "step_1": f"GET /{BASE_URL}/health — you are here",
            "step_2": "copy the link below and paste it into your browser to access the interactive docs to test endpoints in a sandbox UI",
            "docs": f"{BASE_URL}/docs",
            "Disclaimer": "Docs have examples and instructions for each endpoint — always refer back to the Steps at the top of the page when stuck — Thanks for testing!"
        }
    }