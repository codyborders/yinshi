"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from yinshi.api import auth_routes, repos, sessions, workspaces, ws
from yinshi.auth import AuthMiddleware, setup_oauth
from yinshi.config import get_settings
from yinshi.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    settings = get_settings()
    logger.info("Starting %s", settings.app_name)
    init_db()
    setup_oauth()
    yield
    logger.info("Shutdown complete")


settings = get_settings()

app = FastAPI(
    title="Yinshi",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

# Auth
app.add_middleware(AuthMiddleware)

# Routes
app.include_router(auth_routes.router)
app.include_router(repos.router)
app.include_router(workspaces.router)
app.include_router(sessions.router)
app.include_router(ws.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
