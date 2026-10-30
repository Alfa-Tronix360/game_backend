"""Progressao por maturidade do jogador.

Ideia: em vez de mexer nas odds (o que quebraria a verificabilidade), a
maturidade da conta desbloqueia rakeback. O rakeback e um credito explicito,
calculado sobre o volume apostado, e a taxa e sempre a mesma para quem esta
no mesmo nivel. As odds da mesa nao mudam com o nivel.
"""

from dataclasses import dataclass
from decimal import Decimal

MAX_RAKEBACK = Decimal("0.02")  # teto de 2%


@dataclass(frozen=True, slots=True)
class Tier:
    name: str
    threshold: Decimal
    rakeback: Decimal


TIERS: tuple[Tier, ...] = (
    Tier("Bronze", Decimal("0"), Decimal("0.0000")),
    Tier("Prata", Decimal("10000"), Decimal("0.0025")),
    Tier("Ouro", Decimal("50000"), Decimal("0.0050")),
    Tier("Platina", Decimal("200000"), Decimal("0.0075")),
    Tier("Diamante", Decimal("1000000"), Decimal("0.0100")),
    Tier("Elite", Decimal("5000000"), Decimal("0.0150")),
    Tier("Lenda", Decimal("25000000"), Decimal("0.0200")),
)


def current_tier(wagered_total: Decimal) -> Tier:
    tier = TIERS[0]
    for candidate in TIERS:
        if wagered_total >= candidate.threshold:
            tier = candidate
        else:
            break
    return tier


def next_tier(wagered_total: Decimal) -> Tier | None:
    for candidate in TIERS:
        if wagered_total < candidate.threshold:
            return candidate
    return None


def progress_to_next(wagered_total: Decimal) -> Decimal:
    """Fracao de 0 a 1 ate ao proximo nivel."""
    upcoming = next_tier(wagered_total)
    if upcoming is None:
        return Decimal("1")
    previous = current_tier(wagered_total)
    span = upcoming.threshold - previous.threshold
    if span <= 0:
        return Decimal("1")
    done = wagered_total - previous.threshold
    return (done / span).quantize(Decimal("0.0001"))


def estimated_rakeback(wagered_total: Decimal) -> Decimal:
    """Valor que o sistema devolveria hoje, se o jogador apostasse mais nada."""
    return (wagered_total * current_tier(wagered_total).rakeback).quantize(
        Decimal("0.01")
    )
