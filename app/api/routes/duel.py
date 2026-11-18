"""Duelo de dados: jogador contra bot, maior face ganha a ronda."""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.fairness import (
    duel_hmac,
    hash_server_seed,
    roll_d6,
    settle_round,
)
from app.models import Duel, DuelRound, LedgerEntry, ServerSeed, User
from app.schemas.game import (
    DuelHistoryOut,
    DuelOut,
    DuelRollResponse,
    DuelRoundOut,
    DuelStartRequest,
    DuelVerifyRequest,
    MaturityInfo,
    TierInfo,
    UserPublic,
    VerifyResponse,
)
from app.services import maturity

router = APIRouter(prefix="/duel", tags=["duel"])


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


def _seed_for_duel(db: Session, duel: Duel) -> ServerSeed:
    """Semente com que a partida foi aberta.

    Deliberadamente NAO usa a semente activa: a seed fica presa a `duel` no
    arranque, para que rodar a semente a meio da partida nao mude as rondas
    seguintes. Se usasse a activa, revelar a seed antiga depois de uma rotacao
    daria resultados diferentes dos que foram jogados.
    """
    seed = db.get(ServerSeed, duel.server_seed_id)
    if seed is None or seed.user_id != duel.user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Semente da partida em falta.",
        )
    return seed


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


def _to_out(duel: Duel, rounds: list[DuelRound]) -> DuelOut:
    return DuelOut(
        id=duel.id,
        player_side=duel.player_side,
        stake=duel.stake,
        blue_score=duel.blue_score,
        red_score=duel.red_score,
        round_number=duel.round_number,
        status=duel.status,
        server_seed_hash=duel.server_seed_hash,
        payout=duel.payout,
        balance_after=duel.balance_after,
        target_score=settings.duel_target_score,
        rounds=[
            DuelRoundOut.model_validate(r) for r in sorted(rounds, key=lambda r: r.round_number)
        ],
        created_at=duel.created_at,
        finished_at=duel.finished_at,
    )


def _rounds(db: Session, duel_id: int) -> list[DuelRound]:
    return (
        db.query(DuelRound).filter(DuelRound.duel_id == duel_id).all()
    )


def _get_duel(db: Session, duel_id: int, user: User) -> Duel:
    duel = db.get(Duel, duel_id)
    if duel is None or duel.user_id != user.id:
        # Mesma resposta para inexistente e alheio, para nao revelar ids alheios.
        raise HTTPException(status_code=404, detail="Partida nao encontrada.")
    return duel


@router.post("/start", response_model=DuelRollResponse, status_code=status.HTTP_201_CREATED)
def start_duel(
    payload: DuelStartRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelRollResponse:
    """Abre a partida: debita a aposta, fixa o lado e joga a primeira ronda."""
    if payload.stake < settings.min_bet or payload.stake > settings.duel_max_stake:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Aposta entre {settings.min_bet} e {settings.duel_max_stake} para o duelo."
            ),
        )

    active = (
        db.query(Duel)
        .filter(Duel.user_id == user.id, Duel.status == "active")
        .count()
    )
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ja tens uma partida a decorrer. Termina-a ou abandona.",
        )

    if payload.stake > user.balance:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Saldo insuficiente.",
        )

    seed = _active_seed(db, user)

    # Debito da aposta antes de qualquer lancadura, para o saldo nunca ficar negativo.
    # A aposta entra tambem no volume: o duelo e jogo, conta para a maturidade.
    user.balance -= payload.stake
    user.wagered_total += payload.stake

    duel = Duel(
        user_id=user.id,
        player_side=payload.player_side,
        stake=payload.stake,
        blue_score=0,
        red_score=0,
        round_number=0,
        status="active",
        server_seed_id=seed.id,
        server_seed_hash=seed.server_seed_hash,
        balance_after=user.balance,
    )
    db.add(duel)
    db.flush()

    db.add(
        LedgerEntry(
            user_id=user.id,
            kind="duel_stake",
            amount=-payload.stake,
            balance_after=user.balance,
            duel_id=duel.id,
            note=f"Aposta no duelo (joga {'azul' if payload.player_side == 'blue' else 'vermelho'}).",
        )
    )

    result = _play_round(db, duel, user, seed)

    return DuelRollResponse(
        duel=_to_out(duel, _rounds(db, duel.id)),
        round=result["round"],
        user=UserPublic.model_validate(user),
        maturity=_maturity_info(user),
        finished=result["finished"],
    )


def _play_round(
    db: Session, duel: Duel, user: User, seed: ServerSeed
) -> dict:
    """Joga uma ronda. Se a partida acabar, liquida o pote."""
    round_number = duel.round_number + 1

    outcome = settle_round(
        server_seed=seed.server_seed,
        duel_id=duel.id,
        round_number=round_number,
        blue_score=duel.blue_score,
        red_score=duel.red_score,
    )

    duel.blue_score = outcome.blue_score
    duel.red_score = outcome.red_score
    duel.round_number = round_number

    row = DuelRound(
        duel_id=duel.id,
        round_number=round_number,
        blue_roll=outcome.blue,
        red_roll=outcome.red,
        winner=outcome.winner,
        blue_score=outcome.blue_score,
        red_score=outcome.red_score,
    )
    db.add(row)

    finished = False
    target = settings.duel_target_score

    # Um lado so pode atingir o alvo no mesmo instante, porque ganha um ponto
    # por ronda. Ainda assim, a comparacao e feita contra o alvo e nao por
    # "blue == target", para nao depender dessa suposicao.
    if outcome.blue_score >= target or outcome.red_score >= target:
        finished = True
        player_won = (
            outcome.blue_score >= target
            if duel.player_side == "blue"
            else outcome.red_score >= target
        )

        duel.status = "won" if player_won else "lost"
        duel.payout = duel.stake * 2 if player_won else Decimal("0.00")
        duel.finished_at = datetime.now(timezone.utc)
        user.balance += duel.payout
        duel.balance_after = user.balance

        # A entrada da liquidacao regista o dinheiro que se mexeu agora: o
        # pote. A aposta ja foi debitada no arranque, entao nao se volta a
        # subtrair aqui. Vencendo entra o dobro da aposta e a cadeia fecha;
        # perdendo o pote e zero e tambem fecha.
        delta = duel.payout

        db.add(
            LedgerEntry(
                user_id=user.id,
                kind="duel_win" if player_won else "duel_loss",
                amount=delta,
                balance_after=user.balance,
                duel_id=duel.id,
                note=(
                    f"Duelo ganho em {round_number} rondas "
                    f"({duel.blue_score}-{duel.red_score})."
                    if player_won
                    else f"Duelo perdido em {round_number} rondas "
                    f"({duel.blue_score}-{duel.red_score})."
                ),
            )
        )

    db.commit()
    db.refresh(row)
    db.refresh(duel)
    db.refresh(user)

    return {
        "round": DuelRoundOut.model_validate(row),
        "finished": finished,
    }


@router.get("/list", response_model=DuelHistoryOut)
def duel_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelHistoryOut:
    """Partidas terminadas mais recentes primeiro.

    Declarada antes de `/{duel_id}` de proposito: o FastAPI resolve pela ordem
    de declaracao, e se viesse depois o id "list" seria lido como duel_id.
    """
    duels = (
        db.query(Duel)
        .filter(Duel.user_id == user.id, Duel.status != "active")
        .order_by(Duel.id.desc())
        .limit(limit)
        .all()
    )
    return DuelHistoryOut(items=[_to_out(d, _rounds(db, d.id)) for d in duels])


@router.get("/active", response_model=DuelOut | None)
def active_duel(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelOut | None:
    """Partida ainda em curso, ou `null`.

    Permite ao frontend retomar a partida depois de recarregar a pagina. Sem
    isto o jogador ficava preso: o servidor recusava abrir outra ("ja tens uma
    partida a decorrer") e a interface nao sabia qual era para a continuar ou
    abandonar. Declarada antes de `/{duel_id}` pelo mesmo motivo que `/list`.
    """
    duel = (
        db.query(Duel)
        .filter(Duel.user_id == user.id, Duel.status == "active")
        .order_by(Duel.id.desc())
        .first()
    )
    return _to_out(duel, _rounds(db, duel.id)) if duel else None


@router.post("/{duel_id}/roll", response_model=DuelRollResponse)
def roll_round(
    duel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelRollResponse:
    """Joga a ronda seguinte. Recusa se a partida ja tiver acabado."""
    duel = _get_duel(db, duel_id, user)

    if duel.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta partida ja terminou.",
        )

    seed = _seed_for_duel(db, duel)
    result = _play_round(db, duel, user, seed)

    return DuelRollResponse(
        duel=_to_out(duel, _rounds(db, duel.id)),
        round=result["round"],
        user=UserPublic.model_validate(user),
        maturity=_maturity_info(user),
        finished=result["finished"],
    )


@router.get("/{duel_id}", response_model=DuelOut)
def duel_detail(
    duel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelOut:
    duel = _get_duel(db, duel_id, user)
    return _to_out(duel, _rounds(db, duel.id))


@router.post("/{duel_id}/forfeit", response_model=DuelOut)
def forfeit(
    duel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DuelOut:
    """Abandona a partida. A aposta fica perdida, porque ja foi debitada.

    A entrada no ledger tem valor 0: a dinheiro ja saiu do saldo no arranque.
    Debitar a aposta outra vez aqui deixaria a cadeia do ledger a nao fechar com
    o saldo real, e o historico de transacoes mostraria uma perda maior do que a
    perda verdadeira.
    """
    duel = _get_duel(db, duel_id, user)

    if duel.status != "active":
        raise HTTPException(status_code=409, detail="A partida ja terminou.")

    duel.status = "forfeit"
    duel.payout = Decimal("0.00")
    duel.finished_at = datetime.now(timezone.utc)
    duel.balance_after = user.balance

    db.add(
        LedgerEntry(
            user_id=user.id,
            kind="duel_forfeit",
            amount=Decimal("0.00"),
            balance_after=user.balance,
            duel_id=duel.id,
            note=f"Partida abandonada em {duel.round_number} rondas. A aposta fica perdida.",
        )
    )
    db.commit()
    db.refresh(duel)

    return _to_out(duel, _rounds(db, duel.id))


@router.post("/verify", response_model=VerifyResponse)
def verify_round(payload: DuelVerifyRequest) -> VerifyResponse:
    """Recalcula os dois dados de uma ronda a partir da semente revelada."""
    actual_hash = hash_server_seed(payload.server_seed)

    try:
        blue = roll_d6(payload.server_seed, payload.duel_id, payload.round_number, "blue")
        red = roll_d6(payload.server_seed, payload.duel_id, payload.round_number, "red")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="server_seed invalida. Tem de ser hexadecimal.",
        )

    matches = actual_hash == payload.server_seed_hash
    message = (
        f"Azul {blue}, vermelho {red}. "
        + (
            "Hash confirmado: a semente corresponde ao compromisso publicado."
            if matches
            else "Hash diferente: a semente enviada nao corresponde ao compromisso."
        )
    )

    return VerifyResponse(
        expected_hash=payload.server_seed_hash,
        server_seed_hash=actual_hash,
        hash_matches=matches,
        hmac=duel_hmac(payload.server_seed, payload.duel_id, payload.round_number, "blue"),
        roll=blue,
        message=message,
    )

