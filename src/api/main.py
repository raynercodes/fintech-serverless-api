import os
from fastapi import FastAPI
from mangum import Mangum
from src.api.routes import loans, health

app = FastAPI(
    title="Fintech Serverless API",
    description="Small business loan lending platform",
    version="1.0.0",
)

app.include_router(health.router)
app.include_router(loans.router, prefix="/loans", tags=["loans"])

handler = Mangum(app, lifespan="off")
