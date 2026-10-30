from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Bet, LedgerEntry, User
from app.schemas.game import LedgerOut, MaturityInfo, TierInfo, UserPublic
from app.services import maturity

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("/balance", response_model=UserPublic)
def balance(user: User = Depends(get_current_user)) -> UserPublic:
    return UserPublic.model_validate(user)


@router.get("/maturity", response_model=MaturityInfo)
def maturity_info(user: User = Depends(get_current_user)) -> MaturityInfo:
    tier = maturity.current_tier(user.wagered_total)
    upcoming = maturity.next_tier(user.wagered_total)
    return MaturityInfo(
        tier=TierInfo(name=tier.name, threshold=tier.threshold, rakeback=tier.rakeback),
        next_tier=(
            TierInfo(
                name=upcoming.name,
                threshold=upcoming.threshold,
                rakeback=upcoming.rakeback,
            )
            if upcoming
            else None
        ),
        progress=maturity.progress_to_next(user.wagered_total),
        wagered_total=user.wagered_total,
        estimated_rakeback=maturity.estimated_rakeback(user.wagered_total),
    )


@router.get("/transactions", response_model=list[LedgerOut])
def transactions(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LedgerEntry]:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.user_id == user.id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
        .all()
    )
