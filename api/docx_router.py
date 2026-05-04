# api/docx_router.py
# Stub router - docx endpoints (original pipeline)
from fastapi import APIRouter
router = APIRouter(prefix="/docx", tags=["DOCX"])

@router.get("/health")
async def docx_health():
    return {"status": "ok", "service": "docx"}
