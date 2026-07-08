from fastapi import APIRouter
from fastapi.responses import JSONResponse
import json

from src.api.core.health_repository import get_health_status

class PrettyJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(", ", ": ")
        ).encode("utf-8")

router = APIRouter()

@router.get("/health", tags=["health"], response_class=PrettyJSONResponse)
def health_check():
    return get_health_status()
