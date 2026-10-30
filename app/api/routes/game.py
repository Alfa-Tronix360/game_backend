from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, validate_bet
from app.core.config import settings
from app.core.database import get_db
from app.core.fairness import hmac_hexdigest, settle
from app.models import Bet, LedgerEntry, ServerSeed, User
from app.schemas.game import (
    BetOut,
    FairnessProof,
    HistoryOut,
    MaturityInfo,
    RollRequest,
    RollResponse,
    TierInfo,
    UserPublic,
)
from app.services import maturity

router = APIRouter(prefix="/game", tags=["game"])


def _active_seed(db: Session, user: User) -> ServerSeed:
    seed = (
        db.query(ServerSeed)
        .filter(ServerSeed.user_id == user.id, ServerSeed.active.is_(True))
        .order_by(ServerSeed.id.desc())
        .first()
    )
    if seed is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Nenhuma semente activa para esta conta.",
        )
    return seed


def _to_out(bet: Bet, seed: ServerSeed) -> BetOut:
    """Monta a resposta. O HMAC so aparece se a semente ja tiver sido revelada."""
    revealed = seed.revealed_at is not None

    return BetOut(
        id=bet.id,
        mode=bet.mode,
        target=bet.target,
        bet_amount=bet.bet_amount,
        multiplier=bet.multiplier,
        chance=bet.chance,
        roll=bet.roll,
        won=bet.won,
        payout=bet.payout,
        profit=bet.profit,
        balance_after=bet.balance_after,
        proof=FairnessProof(
            server_seed_hash=bet.server_seed_hash,
            client_seed=bet.client_seed,
            nonce=bet.nonce,
            hmac=(
                hmac_hexdigest(seed.server_seed, bet.client_seed, bet.nonce)
                if revealed
                else None
            ),
            roll=bet.roll,
            verifiable=revealed,
        ),
        created_at=bet.created_at,
    )


def _maturity_info(user: User) -> MaturityInfo:
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


@router.post("/roll", response_model=RollResponse, status_code=status.HTTP_201_CREATED)
def place_bet(
    payload: RollRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RollResponse:
    validate_bet(payload.bet_amount, payload.target)

    if payload.bet_amount > user.balance:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Saldo insuficiente.",
        )

    seed = _active_seed(db, user)

    # Debito antes do sorteio, para o saldo nunca ficar negativo.
    user.balance -= payload.bet_amount

    outcome = settle(
        mode=payload.mode,
        target=payload.target,
        bet=payload.bet_amount,
        house_edge=settings.house_edge,
        server_seed=seed.server_seed,
        client_seed=user.client_seed,
        nonce=user.bets_total,
    )

    user.balance += outcome.payout
    user.wagered_total += payload.bet_amount
    user.total_profit += outcome.profit
    user.bets_total += 1

    bet = Bet(
        user_id=user.id,
        mode=payload.mode.value,
        target=payload.target,
        bet_amount=payload.bet_amount,
        multiplier=outcome.multiplier,
        chance=outcome.chance.quantize(Decimal("0.0001")),
        roll=outcome.roll,
        won=outcome.won,
        payout=outcome.payout,
        profit=outcome.profit,
        server_seed_id=seed.id,
        server_seed_hash=seed.server_seed_hash,
        client_seed=user.client_seed,
        nonce=user.bets_total - 1,
        balance_after=user.balance,
    )
    db.add(bet)
    db.flush()

    db.add(
        LedgerEntry(
            user_id=user.id,
            kind="bet_win" if outcome.won else "bet_loss",
            amount=outcome.profit,
            balance_after=user.balance,
            bet_id=bet.id,
            note=f"dados {outcome.roll} | {'abaixo' if payload.mode.value == 'under' else 'acima'} de {payload.target}",
        )
    )

    db.commit()
    db.refresh(bet)
    db.refresh(user)

    return RollResponse(
        **_to_out(bet, seed).model_dump(),
        user=UserPublic.model_validate(user),
        maturity=_maturity_info(user),
    )


@router.get("/history", response_model=HistoryOut)
def bet_history(
    limit: int = Query(default=settings.history_default_limit, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HistoryOut:
    limit = min(limit, settings.history_max_limit)

    bets = (
        db.query(Bet)
        .filter(Bet.user_id == user.id)
        .order_by(Bet.id.desc())
        .limit(limit)
        .all()
    )

    seeds = {
        s.id: s
        for s in db.query(ServerSeed)
        .filter(ServerSeed.user_id == user.id)
        .all()
    }

    items = [
        _to_out(bet, seeds[bet.server_seed_id]) for bet in bets if bet.server_seed_id in seeds
    ]

    return HistoryOut(items=items, has_more=len(bets) == limit)
