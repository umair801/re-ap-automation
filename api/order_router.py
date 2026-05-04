# api/order_router.py
# Stub router - order endpoints (original pipeline)
from fastapi import APIRouter
router = APIRouter(prefix="/orders", tags=["Orders"])

@router.get("/health")
async def order_health():
    return {"status": "ok", "service": "orders"}
