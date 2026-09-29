from fastapi import FastAPI

from app.api.routes import router as api_router
from app.config_api import router as config_api_router
from app.database import Base, engine

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="PyPlayabout API",
    version="0.1.0",
    description="A simple two-tier FastAPI application with a SQLite data layer.",
)


@app.get("/")
def read_root():
    return {
        "app": "PyPlayabout",
        "message": "Welcome to the FastAPI app.",
        "docs_url": "/docs",
    }


app.include_router(api_router)
app.include_router(config_api_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
