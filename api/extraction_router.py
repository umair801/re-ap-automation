# api/extraction_router.py
# Stub router - extraction endpoints (original pipeline)
from fastapi import APIRouter
router = APIRouter(prefix="/extraction", tags=["Extraction"])

@router.get("/health")
async def extraction_health():
    return {"status": "ok", "service": "extraction"}
