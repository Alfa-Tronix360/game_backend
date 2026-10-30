import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt

from app.core.config import settings


def generate_public_id() -> str:
    return uuid.uuid4().hex


def generate_guest_token() -> str:
    """Token de sessao anonima. Basta para moedas ficticias, sem cadastro."""
    return secrets.token_urlsafe(24)


def create_access_token(public_id: str) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes
    )
    payload = {"sub": public_id, "exp": expires_at, "iat": datetime.now(timezone.utc)}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None
    return payload.get("sub")


def money(value: Decimal | str | float) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))
