# Fintech Serverless API

A serverless small business loan lending platform built on AWS Lambda, DynamoDB, and SQS FIFO — modeled after real fintech infrastructure (Kapitus-inspired). Built to demonstrate production-grade serverless architecture, NoSQL data modeling, and compliance-aware system design.

**Live API:** `https://pw4kfpuw3f.execute-api.us-east-1.amazonaws.com/dev`
**Interactive Docs:** `https://pw4kfpuw3f.execute-api.us-east-1.amazonaws.com/dev/docs`
**Author:** [Leonardo Rayner](https://raynercodes.dev) — [GitHub](https://github.com/raynercodes) · [LinkedIn](https://linkedin.com/in/leonardo-rayner-raynercodes/)

---

## What This Is

A full loan application lifecycle — submission, credit-based decisioning, human review, disbursement, and repayment tracking — built entirely on serverless AWS infrastructure. The goal was to build something that reflects how a real fintech company would architect this: encrypted, audited, idempotent, cost-aware, and deployed through a real CI/CD pipeline with human approval gates.

---

## Architecture

```
Client
  ↓
API Gateway (Regional) — Lambda Authorizer (JWT validation)
  ↓
Lambda (FastAPI + Mangum)
  ↓
SQS FIFO Queue ──→ Lambda Worker ──→ DynamoDB (on-demand)
                                          │
                                          ↓
                                  S3 Compliance Vault
                                (Object Lock, 3yr retention)
```

**Compute:** AWS Lambda (Python 3.12, FastAPI + Mangum)
**Database:** DynamoDB on-demand — composite sort key, 2 GSIs, zero table scans
**Queue:** SQS FIFO — two-layer duplicate prevention (SQS dedup + DynamoDB conditional writes) plus an automatic content-hash idempotency layer at the API level
**Auth:** Custom JWT + Lambda Authorizer + progressive brute-force lockout
**Encryption:** AES-256-GCM on PII, PBKDF2-HMAC-SHA256 password hashing (600,000 iterations + pepper), separate customer-managed KMS key per secret category
**Compliance:** S3 Object Lock (COMPLIANCE mode, 3-year retention) — tamper-proof audit trail of every loan lifecycle event
**IaC:** AWS SAM + CloudFormation — single source of truth
**CI/CD:** GitHub Actions (CI) → CodePipeline (CD) with two manual approval gates before production

---

## Core Feature: Credit-Based Loan Decisioning

Every loan application includes a credit score, evaluated automatically by the worker Lambda:

| Score | Outcome |
|---|---|
| ≥ 650 | Auto-approved |
| 500–649 | Routed to human review |
| < 500 | Auto-rejected |

A full lifecycle state machine (`VALID_TRANSITIONS`) governs what a loan is allowed to become next — `pending → review/approved/rejected → funded → repaid/defaulted`. Terminal states (`rejected`, `repaid`, `defaulted`) are permanently locked; a rejected loan can never be un-rejected.

---

## Security Architecture

- **Blast-radius minimization:** six separate customer-managed KMS keys (JWT secret, encryption key, password pepper, DynamoDB at-rest, S3 audit vault, plus two reserved for planned future integrations — see Roadmap)
- **Application-level encryption:** AES-256-GCM on sensitive PII, independent of DynamoDB's own at-rest encryption
- **Password security:** PBKDF2-HMAC-SHA256, 600,000 iterations, per-password salt + a separate pepper held in Secrets Manager
- **Lambda Authorizer:** validates JWTs before the main API function ever invokes, with progressive brute-force lockout (15min → 30min → 1hr → 24hr)
- **Least-privilege IAM:** every Lambda role scoped to only the specific KMS keys, tables, and buckets it actually needs — no wildcard resource grants

---

## Compliance Vault — Immutable Audit Trail

Every meaningful loan lifecycle event (`loan_submitted`, `credit_score_pulled`, `status_changed`, `loan_disbursed`) is written to a dedicated S3 bucket with **Object Lock in COMPLIANCE mode** — once written, a record cannot be deleted or altered by anyone, including account root, for 3 years. Records transition to Glacier Flexible Retrieval after 60 days for cost efficiency without weakening the retention guarantee.

---

## Idempotent Duplicate Detection

Rather than relying solely on SQS FIFO's 5-minute deduplication window (which fails to catch a resubmission arriving 5 minutes and 1 second later), the API automatically fingerprints each loan application's actual content and rejects duplicates within a 90-second window — with zero client cooperation required, matching how the caller population for this API (Swagger UI testers, scripts, future integrations) can't reliably be expected to supply idempotency keys themselves.

---

## Automated Maintenance (EventBridge CRON)

- **Daily stale-transaction sweep:** flags any loan stuck in `pending` for 24+ hours (worker normally resolves this in seconds) for human investigation — never deletes financial records, only flags them
- **Daily DynamoDB backups:** on-demand backups of both critical tables, rolling 35-day retention, pruned automatically

---

## API Endpoints

**Auth** — public, no token required
| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` | Create a new user account |
| POST | `/auth/login` | Authenticate, receive a 15-minute JWT |
| POST | `/auth/demo` | Get a JWT for the self-healing demo account — no registration needed |

**Loans** — all require a Bearer JWT
| Method | Path | Description |
|---|---|---|
| POST | `/loans/` | Submit a loan application (credit score included); auto-decisioned async via SQS + worker |
| GET | `/loans/{loan_id}` | Fetch a single loan by its transaction ID |
| GET | `/loans/account/{account_id}` | All loans for one account |
| GET | `/loans/customer/{customer_id}` | All loans across every account belonging to a customer |
| PATCH | `/loans/{loan_id}/status` | Move a loan through its lifecycle (e.g. review → approved → funded) — enforced by the state machine |

**Health** — public
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Service status, version, and stack metadata |

Full request/response schemas, examples, and try-it-out testing are available at `/docs`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Compute | AWS Lambda, Python 3.12, FastAPI, Mangum |
| Database | DynamoDB (on-demand), 2 GSIs |
| Queue | SQS FIFO |
| Auth | Custom JWT, Lambda Authorizer |
| Encryption | AES-256-GCM, PBKDF2-HMAC-SHA256, AWS KMS |
| Storage | S3 (Object Lock, Glacier lifecycle) |
| IaC | AWS SAM, CloudFormation |
| CI/CD | GitHub Actions, AWS CodePipeline, CodeBuild |
| Testing | pytest, moto |
| Observability | CloudWatch (structured logs, 14-day retention), X-Ray |

---

## Testing

60+ tests covering loan lifecycle transitions, credit decisioning boundaries, idempotency/duplicate detection, auth flows, the Lambda Authorizer, and scheduled job logic — using `moto` to simulate AWS services rather than hitting real infrastructure.

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

---

## Deployment

```bash
cp -r src ./package/
sam build --template infrastructure/template.yaml --no-cached
sam deploy --parameter-overrides Environment=dev
```

Dev deploys directly for fast local iteration. Staging and production are gated behind a CodePipeline with mandatory manual approval — mirroring how a regulated financial platform would actually control changes to customer-facing infrastructure.

---

## Roadmap / In Progress

Being upfront about what's still moving:

- **Custom domain (`fintech.raynercodes.dev`) via CloudFront** — ACM certificate is issued and validated; CloudFront distribution creation is currently blocked on an AWS account verification review (submitted, awaiting AWS response — standard for newer AWS accounts, not a configuration issue)
- **Full CI/CD pipeline run through production** — pipeline is built and has been validated through the staging deploy stage; the CodeBuild stage is currently blocked by an AWS account-level build-queue limit (support case open)
- **1,000-concurrent-user load test with Locust** — locustfile is built and validated at small scale; the full run is blocked on a Lambda concurrent-execution quota increase (default account limit is well below what's needed; quota increase requested)
- Merge `dev` → `main` once the above clear and a full production deploy is confirmed stable

These are all real AWS account-level reviews with no published SLA — not application bugs. Support cases are open and being tracked.

---

## Non-Implemented / Deliberate Scope Decisions

- **AWS Cognito** — skipped in favor of a custom JWT implementation, to demonstrate deeper auth mechanics
- **ElastiCache / Redis** — skipped; a 3-tier cache (CloudFront edge → Lambda execution context → DynamoDB cache table) covers the same need at zero idle cost
- **API Gateway stage caching** — skipped; billed hourly regardless of traffic, conflicts with serverless pay-per-use design
- **Distinct reviewer/underwriter role** — audit trail currently records the requester's own identity as the "actor" on all events; a production system would require a separate authenticated reviewer role, distinct from the applicant
- **`db-credentials` / `api-keys` KMS keys and secrets** — provisioned ahead of need, reserved for a future RDS migration and third-party integrations (Stripe, credit bureau) respectively; not yet wired into any code path

---

## License

Personal portfolio project. Not licensed for production use by third parties.