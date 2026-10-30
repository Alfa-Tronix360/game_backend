from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.fairness import (
    generate_server_seed,
    hash_server_seed,
    hmac_hexdigest,
    verify_roll,
)
from app.models import ServerSeed, User
from app.schemas.game import (
    ClientSeedOut,
    CurrentFairness,
    RotateSeedResponse,
    SetClientSeedRequest,
    VerifyRequest,
    VerifyResponse,
)

router = APIRouter(prefix="/fairness", tags=["fairness"])


@router.get("/current", response_model=CurrentFairness)
def current_fairness(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurrentFairness:
    """Estado da prova: hash publicado, semente ativa e seed do jogador."""
    seed = (
        db.query(ServerSeed)
        .filter(ServerSeed.user_id == user.id, ServerSeed.active.is_(True))
        .order_by(ServerSeed.id.desc())
        .first()
    )
    if seed is None:
        raise HTTPException(status_code=404, detail="Sem semente activa.")

    return CurrentFairness(
        server_seed_hash=seed.server_seed_hash,
        client_seed=user.client_seed,
        nonce=user.bets_total,
        active_since=seed.created_at,
        revealed=seed.revealed_at is not None,
    )


@router.post("/seed/rotate", response_model=RotateSeedResponse)
def rotate_server_seed(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RotateSeedResponse:
    """Revela a semente actual e cria uma nova.

    A semente revelada valida todas as apostas feitas com ela. Como o hash foi
    publicado antes dessas apostas, o servidor ja estava comprometido com o
    resultado quando as aceitou.
    """
    active = (
        db.query(ServerSeed)
        .filter(ServerSeed.user_id == user.id, ServerSeed.active.is_(True))
        .order_by(ServerSeed.id.desc())
        .first()
    )

    revealed = ""
    rotated = False
    if active is not None:
        active.active = False
        active.revealed_at = datetime.now(timezone.utc)
        revealed = active.server_seed
        rotated = True

    raw_seed = generate_server_seed()
    db.add(
        ServerSeed(
            user_id=user.id,
            server_seed=raw_seed,
            server_seed_hash=hash_server_seed(raw_seed),
        )
    )
    db.commit()

    return RotateSeedResponse(
        server_seed=revealed,
        server_seed_hash=hash_server_seed(revealed) if revealed else "",
        client_seed=user.client_seed,
        rotated=rotated,
    )


@router.post("/client-seed", response_model=ClientSeedOut)
def set_client_seed(
    payload: SetClientSeedRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClientSeedOut:
    """Troca a semente do jogador. O nonce NAO reinicia.

    Reiniciar o nonce permitiria repetir a mesma aposta com a mesma semente e o
    mesmo numero, o que daria ao servidor o direito de escolher o resultado.
    """
    user.client_seed = payload.client_seed
    db.commit()
    return ClientSeedOut(client_seed=user.client_seed, nonce=user.bets_total)


@router.post("/verify", response_model=VerifyResponse)
def verify_roll_endpoint(payload: VerifyRequest) -> VerifyResponse:
    """Recalcula um resultado no servidor, a partir da semente revelada."""
    try:
        actual_hash = hash_server_seed(payload.server_seed)
        roll = verify_roll(payload.server_seed, payload.client_seed, payload.nonce)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="server_seed invalida. Tem de ser hexadecimal.",
        )

    digest = hmac_hexdigest(payload.server_seed, payload.client_seed, payload.nonce)
    matches = actual_hash == payload.server_seed_hash

    return VerifyResponse(
        expected_hash=payload.server_seed_hash,
        server_seed_hash=actual_hash,
        hash_matches=matches,
        hmac=digest,
        roll=roll,
        message=(
            "Hash confirmado. A semente revela corresponde ao compromisso publicado, "
            "e o resultado pode ser recalcado de forma independente."
            if matches
            else "Hash diferente. A semente enviada nao corresponde ao compromisso "
            "publicado antes das apostas."
        ),
    )
