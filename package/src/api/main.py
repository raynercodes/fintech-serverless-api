import os
from fastapi import FastAPI
from mangum import Mangum
from src.api.routes import loans, health

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")

app = FastAPI(
    title="Fintech Serverless API",
    description="""
## Small Business Loan Lending Platform

Serverless fintech API built on AWS Lambda, DynamoDB, and SQS FIFO.

### Features
- Loan application submission and tracking
- Async transaction processing via SQS FIFO
- JWT authentication with Lambda Authorizer
- AES-256-GCM encryption on sensitive PII
- L1/L2 caching strategy
- CodeDeploy canary deployments

### Auth
All endpoints except `/health` require a Bearer JWT token in the Authorization header.
    """,
    version="1.0.0",
    contact={
        "name": "Leonardo Rayner",
        "url": "https://raynercodes.dev",
        "email": "raynercodes@gmail.com",
        "LinkedIn": "https://www.linkedin.com/in/leonardo-rayner-raynercodes/",
    }
)

app.include_router(health.router)
app.include_router(loans.router, prefix="/loans", tags=["loans"])

handler = Mangum(app, lifespan="off")
