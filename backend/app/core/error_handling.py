"""
Never leak stack traces, file paths, or internal exception messages to the
client (spec §33). FastAPI's own HTTPException already returns a clean
{"detail": ...} — this handler is the catch-all for anything ELSE that
escapes a route (an unhandled exception, a provider blowing up in a way we
didn't anticipate), which otherwise would hand back a 500 with traceback
detail in some configurations.
"""
import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("atla.errors")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = str(uuid.uuid4())
    logger.exception("Unhandled exception [error_id=%s] on %s %s", error_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our end.", "error_id": error_id},
    )
