from decimal import Decimal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.fairness import RollMode, win_chance
from app.core.security import decode_access_token
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessao em falta.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    public_id = decode_access_token(credentials.credentials)
    if public_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessao invalida ou expirada.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.public_id == public_id).one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Utilizador nao encontrado."
        )
    return user


def validate_bet(bet_amount: Decimal, target: Decimal) -> None:
    if bet_amount < settings.min_bet:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Aposta minima: {settings.min_bet}.",
        )
    if bet_amount > settings.max_bet:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Aposta maxima: {settings.max_bet}.",
        )
    if not (settings.min_chance <= target <= settings.max_chance):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A percentagem tem de estar entre {settings.min_chance} "
                f"e {settings.max_chance}."
            ),
        )
