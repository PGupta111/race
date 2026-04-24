"""Bearer token authentication dependency."""
import logging
import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_TOKEN  = os.getenv("RACE_API_TOKEN", "")

if not _TOKEN:
    _TOKEN = secrets.token_hex(16)
    logger.warning(
        "RACE_API_TOKEN not set — generated ephemeral token: %s  "
        "(set it in .env for a persistent token)",
        _TOKEN,
    )


async def require_token(
    creds: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    if creds is None or creds.credentials != _TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
