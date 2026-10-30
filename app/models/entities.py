from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.fairness import RollMode


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    wagered_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    bets_total: Mapped[int] = mapped_column(Integer, default=0)
    total_profit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))

    client_seed: Mapped[str] = mapped_column(String(128), default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServerSeed(Base):
    """Par (semente secreta, hash publicado).

    A semente nunca muda sem rotacao explicita do jogador. O hash fica gravado
    para sempre, para que as apostas feitas com ela continuem verificaveis.
    """

    __tablename__ = "server_seeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    server_seed: Mapped[str] = mapped_column(String(64))
    server_seed_hash: Mapped[str] = mapped_column(String(64), index=True)

    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    mode: Mapped[str] = mapped_column(String(8))
    target: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    bet_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    multiplier: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    chance: Mapped[Decimal] = mapped_column(Numeric(6, 4))

    roll: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    won: Mapped[bool] = mapped_column(Boolean)
    payout: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    profit: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    # Prova de justeza, suficiente para recalcular o resultado.
    server_seed_id: Mapped[int] = mapped_column(ForeignKey("server_seeds.id"))
    server_seed_hash: Mapped[str] = mapped_column(String(64))
    client_seed: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[int] = mapped_column(Integer)

    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class LedgerEntry(Base):
    """Rasto de auditoria de cada movimentacao de saldo."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    bet_id: Mapped[int | None] = mapped_column(
        ForeignKey("bets.id", ondelete="SET NULL"), nullable=True
    )
    duel_id: Mapped[int | None] = mapped_column(
        ForeignKey("duels.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class Duel(Base):
    """Uma partida entre o jogador e o bot.

    A aposta e debitada ao abrir a partida. Se a partida terminar, quem ganha
    recebe o pote inteiro; se for abandonada, fica registado como forfeit.
    """

    __tablename__ = "duels"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # "blue" quando o jogador e o azul; "red" quando e o vermelho.
    player_side: Mapped[str] = mapped_column(String(4))
    stake: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    blue_score: Mapped[int] = mapped_column(Integer, default=0)
    red_score: Mapped[int] = mapped_column(Integer, default=0)
    round_number: Mapped[int] = mapped_column(Integer, default=0)

    # "active" | "won" | "lost" | "forfeit"
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)

    # Compromisso de justeza: hash publicado antes da primeira ronda.
    server_seed_id: Mapped[int] = mapped_column(ForeignKey("server_seeds.id"))
    server_seed_hash: Mapped[str] = mapped_column(String(64))

    payout: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DuelRound(Base):
    """Uma ronda do duelo: os dois dados e quem ficou com o ponto."""

    __tablename__ = "duel_rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    duel_id: Mapped[int] = mapped_column(
        ForeignKey("duels.id", ondelete="CASCADE"), index=True
    )
    round_number: Mapped[int] = mapped_column(Integer)

    blue_roll: Mapped[int] = mapped_column(Integer)
    red_roll: Mapped[int] = mapped_column(Integer)
    # "blue" | "red" | "draw"
    winner: Mapped[str] = mapped_column(String(4))

    blue_score: Mapped[int] = mapped_column(Integer)
    red_score: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
