from fastapi import APIRouter
router = APIRouter(prefix="/reports", tags=["reports"])

@router.get("/generate")
async def generate_report(period_start: str, period_end: str, format: str = "pdf"):
    return {"status": "generated", "period_start": period_start,
            "period_end": period_end, "format": format}
