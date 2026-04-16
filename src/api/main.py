import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.core.config import get_settings
from src.core.database import engine
from src.core.logging import setup_logging, get_logger
from src.models.db import Base
from src.models.schemas import ProblemDetail, ErrorDetail
from src.api.routes import process, status, health, metrics

setup_logging()
logger = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(lambda conn: Base.metadata.create_all(conn, checkfirst=True))

    import asyncio
    from src.services.embeddings import get_embedder
    await asyncio.get_event_loop().run_in_executor(None, get_embedder)

    logger.info("api_ready")
    yield
    await engine.dispose()


app = FastAPI(
    title="Prompt Processing System",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = [
        ErrorDetail(field=".".join(str(l) for l in e["loc"]), msg=e["msg"])
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            type="https://errors.promptsvc.io/validation-error",
            title="Validation failed",
            status=422,
            detail=details,
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content=ProblemDetail(
            type="https://errors.promptsvc.io/internal-error",
            title="Internal server error",
            status=500,
        ).model_dump(),
    )


app.include_router(process.router, prefix=settings.api_v1_prefix)
app.include_router(status.router, prefix=settings.api_v1_prefix)
app.include_router(health.router, prefix=settings.api_v1_prefix)
app.include_router(metrics.router, prefix=settings.api_v1_prefix)