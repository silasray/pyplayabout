from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/health")
def health_check():
    return {"status": "ok", "service": "pyplayabout-api"}
