# api/iif_router.py
# FastAPI endpoint for QuickBooks Desktop IIF file generation

import logging
from datetime import date
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from integrations.iif_generator import get_iif_generator, IIFBatch, ApprovedBill, BillLineItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/exports", tags=["IIF Export"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class IIFGenerationResult(BaseModel):
    entity_name: str
    file_path: str
    bill_count: int
    valid: bool
    errors: list[str]
    generation_date: str


# ---------------------------------------------------------------------------
# GET /exports/iif/{entity_name}
# Daily IIF generation trigger for one entity
# ---------------------------------------------------------------------------

@router.get(
    "/iif/{entity_name}",
    response_model=IIFGenerationResult,
    summary="Generate daily IIF file for a QB Desktop entity",
)
async def generate_iif_for_entity(
    entity_name: str,
    as_of_date: Optional[str] = Query(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today.",
    ),
):
    """
    Trigger IIF batch file generation for the given entity.

    This endpoint is called by the daily cron job on the office mini-PC.
    It queries approved bills from Airtable (stub — wired in GAP 3),
    generates the IIF file, and saves it to exports/iif/{entity_name}/YYYY-MM-DD.iif.

    The operator then opens that entity's QB Desktop file and imports the IIF.

    NOTE: This endpoint does NOT initiate payments. It only produces the import file.
    """
    # Parse date
    if as_of_date:
        try:
            generation_date = date.fromisoformat(as_of_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date format: '{as_of_date}'. Use YYYY-MM-DD.",
            )
    else:
        generation_date = date.today()

    generator = get_iif_generator()

    # TODO (GAP 3): Replace this stub with real Airtable query
    # bills = airtable_client.get_approved_bills(entity_name=entity_name, as_of=generation_date)
    # For now, return an informative response indicating the endpoint is live
    bills = []  # Will be populated when Airtable client (GAP 3) is wired in

    batch = IIFBatch(
        entity_name=entity_name,
        generation_date=generation_date,
        bills=bills,
    )

    file_path = generator.generate(batch)

    if not file_path:
        return IIFGenerationResult(
            entity_name=entity_name,
            file_path="",
            bill_count=0,
            valid=True,
            errors=[],
            generation_date=str(generation_date),
        )

    validation = generator.validate_iif_file(file_path)

    logger.info(
        f"IIF generation endpoint: entity={entity_name} | "
        f"date={generation_date} | bills={len(bills)} | "
        f"valid={validation['valid']}"
    )

    return IIFGenerationResult(
        entity_name=entity_name,
        file_path=file_path,
        bill_count=validation["bill_count"],
        valid=validation["valid"],
        errors=validation["errors"],
        generation_date=str(generation_date),
    )


# ---------------------------------------------------------------------------
# GET /exports/iif/{entity_name}/validate
# Validate an already-generated IIF file
# ---------------------------------------------------------------------------

@router.get(
    "/iif/{entity_name}/validate",
    summary="Validate the most recent IIF file for an entity",
)
async def validate_iif_file(
    entity_name: str,
    as_of_date: Optional[str] = Query(default=None),
):
    """
    Run structural validation on an already-generated IIF file.
    Checks TRNS/ENDTRNS balance and returns bill and SPL line counts.
    """
    if as_of_date:
        try:
            generation_date = date.fromisoformat(as_of_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Use YYYY-MM-DD format.")
    else:
        generation_date = date.today()

    generator = get_iif_generator()
    file_path = str(generator._get_output_path(entity_name, generation_date))

    result = generator.validate_iif_file(file_path)
    result["file_path"] = file_path
    result["entity_name"] = entity_name
    result["generation_date"] = str(generation_date)

    return result
