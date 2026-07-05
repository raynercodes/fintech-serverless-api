import os
from fastapi import FastAPI
from fastapi.security import HTTPBearer
from mangum import Mangum
from src.api.routes import loans, health, auth

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

# Declares the Bearer auth scheme so Swagger UI renders the Authorize button
# Actual enforcement happens at the Lambda Authorizer — this is documentation only
bearer_scheme = HTTPBearer()

app = FastAPI(
    title="Fintech Serverless API",
    description="""
## Small Business Loan Lending Platform

Serverless fintech API built on AWS Lambda, DynamoDB, and SQS FIFO — modeled after real fintech infrastructure.

---

## 🚀 Quick Start — Test in 30 Seconds

Note: Always refer back to the Steps at the top of the page when stuck.

**Step 1:** Scroll down and click on `POST /auth/demo` once your there **click** `Try it out` then **click** `Execute` — no registration needed, returns a JWT instantly

**Step 2:** Scroll down and copy the `access_token` from the HTTP 200 response — only copy the value inside the quotes, not the quotes themselves

**Step 3:** Click the **Authorize** button above, paste the token, click **Authorize** — Should be a lock right beside the words "Authorize" Click that one.

**Step 4:** Click the x button. Do not click the "Logout" button — that will remove your token and you'll have to repeat Step 1 again. — If you don't see the logout button refer to step 3 and click the authorize button once the token is pasted

**Step 5:** Test any endpoint — submit a loan application, check its status, query by account — routes have specified examples and instructions for each endpoint

**Disclaimer:** Scroll down to read each endpoint's response as this is where your metadata is returned — transaction IDs, timestamps, and status updates are all included in the response

---

## Architecture
- **Compute:** AWS Lambda + FastAPI + Mangum
- **Database:** DynamoDB on-demand — composite sort key, 2 GSIs, zero table scans
- **Queue:** SQS FIFO — two-layer duplicate prevention
- **Auth:** JWT + Lambda Authorizer + brute force progressive lockout
- **Encryption:** AES-256-GCM on sensitive PII — separate KMS key per secret category
- **Caching:** L0 CloudFront + L1 Lambda execution context + L2 DynamoDB
- **Deployment:** CodeDeploy canary 10%/15min with automated rollback
- **Pipeline:** GitHub Actions CI → CodePipeline CD — two manual approval gates

---

## Auth
All endpoints except `/health` and `/auth/*` require a Bearer JWT token.
Tokens expire in **15 minutes** — hit `/auth/demo` again for a fresh one.
""",
    version="1.0.0",
    contact={
        "name": "Leonardo Rayner",
        "url": "https://pw4kfpuw3f.execute-api.us-east-1.amazonaws.com/dev/health",
        "email": "raynercodes@gmail.com",
        "LinkedIn": "https://www.linkedin.com/in/leonardo-rayner-raynercodes/",
    },
    root_path=f"/{ENVIRONMENT}"
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(loans.router, prefix="/loans", tags=["loans"])

handler = Mangum(app, lifespan="off")
