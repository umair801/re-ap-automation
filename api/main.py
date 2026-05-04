# api/main.py

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from api.ingestion_router import router as ingestion_router
from api.approval_router import router as approval_router
from api.metrics_router import router as metrics_router
from api.docusign_router import router as docusign_router
from api.iif_router import router as iif_router
from api.extraction_router import router as extraction_router
from api.order_router import router as order_router
from api.docx_router import router as docx_router
from core.config import get_settings
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

logger = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "ap_ai_startup",
        service="AP-AI Accounts Payable Automation Agent",
        brand="Datawebify",
        version="1.0.0",
    )
    yield
    logger.info("ap_ai_shutdown")


app = FastAPI(
    title="AP-AI: Enterprise Accounts Payable Automation Agent",
    description=(
        "Autonomous invoice ingestion, extraction, validation, three-way match, "
        "approval routing, payment scheduling, and ERP sync. "
        "Built by Datawebify."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ------------------------------------------------------------------
# CORS
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Routers
# ------------------------------------------------------------------
app.include_router(ingestion_router)
app.include_router(approval_router)
app.include_router(metrics_router)
app.include_router(docusign_router)
app.include_router(iif_router)
app.include_router(extraction_router)
app.include_router(order_router)
app.include_router(docx_router)

templates = Jinja2Templates(directory="templates")


@app.get("/upload", tags=["Schedule Extraction"])
async def upload_page(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.get("/docx-ui", tags=["DOCX Standardization"])
async def docx_ui_page(request: Request):
    return templates.TemplateResponse("docx_ui.html", {"request": request})


# ------------------------------------------------------------------
# Root
# ------------------------------------------------------------------
@app.get("/", tags=["Root"])
async def root():
    return {
        "service": "AP Real Estate Automation System",
        "version": "1.0.0",
        "brand": "Datawebify",
        "docs": "/docs",
        "health": "/metrics/health",
        "status": "online"
    }
