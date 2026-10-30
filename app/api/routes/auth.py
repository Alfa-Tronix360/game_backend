from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.fairness import generate_server_seed, hash_server_seed
from app.core.security import create_access_token, generate_public_id
from app.models import LedgerEntry, ServerSeed, User
from app.schemas.game import GuestSession, UserPublic

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/guest", response_model=GuestSession, status_code=status.HTTP_201_CREATED)
def create_guest_session(db: Session = Depends(get_db)) -> GuestSession:
    """Abre uma sessao de moedas ficticias. Sem cadastro, sem password."""
    public_id = generate_public_id()

    user = User(
        public_id=public_id,
        balance=settings.starting_balance,
        wagered_total=Decimal("0.00"),
        total_profit=Decimal("0.00"),
        client_seed=generate_public_id(),
    )
    db.add(user)
    db.flush()

    # Primeira semente: o hash e publicado antes de existir qualquer aposta.
    raw_seed = generate_server_seed()
    db.add(
        ServerSeed(
            user_id=user.id,
            server_seed=raw_seed,
            server_seed_hash=hash_server_seed(raw_seed),
        )
    )

    db.add(
        LedgerEntry(
            user_id=user.id,
            kind="bonus",
            amount=settings.starting_balance,
            balance_after=settings.starting_balance,
            note="Saldo inicial de demonstracao.",
        )
    )
    db.commit()
    db.refresh(user)

    token, expires_at = create_access_token(public_id)
    return GuestSession(
        token=token,
        expires_at=expires_at,
        user=UserPublic.model_validate(user),
    )
