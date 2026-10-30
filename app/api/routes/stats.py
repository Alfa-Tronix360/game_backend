from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Bet, User
from app.schemas.game import StatsOut

router = APIRouter(prefix="/stats", tags=["stats"])

BUCKETS = 10


def _histogram(bets: list[Bet]) -> dict[str, int]:
    """Distribuicao de resultados em 10 faixas de 10 pontos (0-9, 10-19, ...)."""
    buckets = [0] * BUCKETS
    for bet in bets:
        index = min(int(bet.roll) // 10, BUCKETS - 1)
        buckets[index] += 1
    return {f"{i * 10}-{i * 10 + 9}": count for i, count in enumerate(buckets)}


@router.get("/me", response_model=StatsOut)
def my_stats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatsOut:
    bets = (
        db.query(Bet).filter(Bet.user_id == user.id).order_by(Bet.id.desc()).all()
    )

    if not bets:
        return StatsOut(
            total_bets=0,
            wins=0,
            losses=0,
            win_rate=Decimal("0"),
            total_wagered=Decimal("0.00"),
            net_profit=Decimal("0.00"),
            best_multiplier=None,
            biggest_win=None,
            current_streak=0,
            streak_type="none",
            average_roll=None,
            average_multiplier=None,
            roll_histogram={},
        )

    wins = [b for b in bets if b.won]
    total = len(bets)

    # Sequencia atual: conta a partir da aposta mais recente para tras.
    streak_type = "win" if bets[0].won else "loss"
    streak = 0
    for bet in bets:
        if (bet.won and streak_type == "win") or (not bet.won and streak_type == "loss"):
            streak += 1
        else:
            break

    win_multipliers = [b.multiplier for b in wins]
    avg_roll = (sum(b.roll for b in bets) / total).quantize(Decimal("0.01"))

    return StatsOut(
        total_bets=total,
        wins=len(wins),
        losses=total - len(wins),
        win_rate=(Decimal(len(wins)) / Decimal(total) * 100).quantize(Decimal("0.01")),
        total_wagered=sum(b.bet_amount for b in bets),
        net_profit=sum(b.profit for b in bets),
        best_multiplier=max(win_multipliers) if win_multipliers else None,
        biggest_win=max((b.profit for b in wins), default=None),
        current_streak=streak,
        streak_type=streak_type,
        average_roll=avg_roll,
        average_multiplier=(
            (sum(win_multipliers) / len(win_multipliers)).quantize(Decimal("0.01"))
            if win_multipliers
            else None
        ),
        roll_histogram=_histogram(bets),
    )
