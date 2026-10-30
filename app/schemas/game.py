"""Esquemas de entrada e saida da API.

Dinheiro e enviado como numero JSON, nao como string. O Pydantic v2 serializa
`Decimal` como string por omissao, o que obrigaria o frontend a converter em
cada sitio; aqui declara-se explicitamente que se quer numero.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.core.fairness import RollMode

# Valor monetario devolvido como numero JSON.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v), return_type=float, when_used="json"),
]

# Percentagens de vitoria: mesma coisa, mas o valor e lido directamente.
Percent = Money


# --- entrada ---------------------------------------------------------------


class RollRequest(BaseModel):
    bet_amount: Decimal = Field(gt=0, decimal_places=2, max_digits=18)
    mode: RollMode
    target: Decimal = Field(
        decimal_places=2,
        max_digits=6,
        description="Corte em percentagem. under ganha se roll < target.",
    )


class SetClientSeedRequest(BaseModel):
    client_seed: str = Field(min_length=1, max_length=128)


class VerifyRequest(BaseModel):
    server_seed: str = Field(min_length=8, max_length=128)
    server_seed_hash: str = Field(min_length=64, max_length=64)
    client_seed: str = Field(max_length=128)
    nonce: int = Field(ge=0)


# --- saida -----------------------------------------------------------------


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: str
    balance: Money
    wagered_total: Money
    bets_total: int
    total_profit: Money
    client_seed: str


class FairnessProof(BaseModel):
    """Prova de justeza de uma aposta.

    `hmac` e None enquanto a semente estiver activa, porque calcula-lo exige a
    semente secreta. E esse o ponto do esquema: o servidor publicou o hash
    antes de aceitar a aposta, por isso ja estava comprometido com o
    resultado e nao o pode mudar sem ser apanhado. Depois da rotacao, o
    jogador tem tudo o que precisa para refazer a conta fora do servidor.
    """

    server_seed_hash: str
    client_seed: str
    nonce: int
    hmac: str | None = None
    roll: Money
    verifiable: bool


class BetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mode: RollMode
    target: Money
    bet_amount: Money
    multiplier: Money
    chance: Money
    roll: Money
    won: bool
    payout: Money
    profit: Money
    balance_after: Money
    proof: FairnessProof
    created_at: datetime


class TierInfo(BaseModel):
    name: str
    threshold: Money
    rakeback: Percent


class MaturityInfo(BaseModel):
    tier: TierInfo
    next_tier: TierInfo | None
    progress: Percent
    wagered_total: Money
    estimated_rakeback: Money
    note: str = (
        "O rakeback e um credito separado. As odds da mesa sao iguais em todos "
        "os niveis; a maturidade nunca mexe na probabilidade de ganhar."
    )


class RollResponse(BetOut):
    user: UserPublic
    maturity: MaturityInfo


class HistoryOut(BaseModel):
    items: list[BetOut]
    has_more: bool


class StatsOut(BaseModel):
    total_bets: int
    wins: int
    losses: int
    win_rate: Percent
    total_wagered: Money
    net_profit: Money
    best_multiplier: Money | None
    biggest_win: Money | None
    current_streak: int
    streak_type: str
    average_roll: Money | None
    average_multiplier: Money | None
    roll_histogram: dict[str, int]


class LedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    amount: Money
    balance_after: Money
    note: str
    created_at: datetime


class VerifyResponse(BaseModel):
    expected_hash: str
    server_seed_hash: str
    hash_matches: bool
    hmac: str
    roll: Money
    message: str


class RotateSeedResponse(BaseModel):
    server_seed: str
    server_seed_hash: str
    client_seed: str
    rotated: bool


class GuestSession(BaseModel):
    token: str
    expires_at: datetime
    user: UserPublic


class CurrentFairness(BaseModel):
    server_seed_hash: str
    client_seed: str
    nonce: int
    active_since: datetime
    revealed: bool


class ClientSeedOut(BaseModel):
    client_seed: str
    nonce: int


class HealthOut(BaseModel):
    status: str
    version: str
    house_edge: Money
    min_chance: Money
    max_chance: Money
    min_bet: Money
    max_bet: Money
    duel_max_stake: Money
    duel_target_score: int


# --- duelo -----------------------------------------------------------------

Side = Literal["blue", "red"]
RoundWinner = Literal["blue", "red", "draw"]
DuelStatus = Literal["active", "won", "lost", "forfeit"]


class DuelStartRequest(BaseModel):
    stake: Decimal = Field(gt=0, decimal_places=2, max_digits=18)
    player_side: Side = Field(default="blue")


class DuelRoundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    round_number: int
    blue_roll: int
    red_roll: int
    winner: RoundWinner
    blue_score: int
    red_score: int


class DuelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    player_side: Side
    stake: Money
    blue_score: int
    red_score: int
    round_number: int
    status: DuelStatus
    server_seed_hash: str
    payout: Money
    balance_after: Money
    target_score: int
    rounds: list[DuelRoundOut]
    created_at: datetime
    finished_at: datetime | None


class DuelRollResponse(BaseModel):
    duel: DuelOut
    round: DuelRoundOut
    user: UserPublic
    maturity: MaturityInfo
    finished: bool = Field(description="True quando a partida terminou nesta ronda.")


class DuelHistoryOut(BaseModel):
    items: list[DuelOut]


class DuelVerifyRequest(BaseModel):
    """Recalcula uma ronda a partir da semente revelada."""

    server_seed: str = Field(min_length=8, max_length=128)
    server_seed_hash: str = Field(min_length=64, max_length=64)
    duel_id: int = Field(ge=1)
    round_number: int = Field(ge=1)
