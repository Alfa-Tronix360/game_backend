"""Testes de integracao da API: sessao, apostas, historico e verificacao."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.core.config import settings
from app.core.fairness import compute_roll, hash_server_seed
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """App com uma base de dados isolada por teste."""
    test_db = tmp_path / "teste.db"
    engine = create_engine(
        f"sqlite:///{test_db}", connect_args={"check_same_thread": False}
    )
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)
    database.Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def guest(client) -> dict:
    response = client.post("/api/v1/auth/guest")
    assert response.status_code == 201
    return response.json()


def auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['token']}"}


def dec(value) -> Decimal:
    """Dinheiro chega como numero JSON; o Decimal compara certo so via texto.

    `1.98 == Decimal("1.98")` e False em Python, porque o float 1.98 nao e
    exactamente 1.98 em base 2. Passar por str evita essa comparacao.
    """
    return Decimal(str(value))


# --- sessao ----------------------------------------------------------------


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert dec(body["house_edge"]) == settings.house_edge


def test_guest_session_starts_with_play_money(client):
    session = guest(client)
    assert dec(session["user"]["balance"]) == settings.starting_balance
    assert session["user"]["bets_total"] == 0
    assert session["user"]["client_seed"]


def test_request_without_token_is_rejected(client):
    response = client.get("/api/v1/wallet/balance")
    assert response.status_code == 401


def test_bogus_token_is_rejected(client):
    response = client.get(
        "/api/v1/wallet/balance", headers={"Authorization": "Bearer lixo"}
    )
    assert response.status_code == 401


# --- apostas ---------------------------------------------------------------


def test_roll_updates_balance_and_keeps_odds(client):
    session = guest(client)
    start = Decimal(session["user"]["balance"])

    response = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "10.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    )
    assert response.status_code == 201
    body = response.json()

    assert dec(body["multiplier"]) == Decimal("1.98")
    assert dec(body["chance"]) == Decimal("0.5")
    assert Decimal("0.00") <= dec(body["roll"]) < Decimal("100.00")

    expected = start + (dec(body["payout"]) - Decimal("10.00"))
    assert dec(body["balance_after"]) == expected
    assert dec(body["user"]["balance"]) == expected


def test_roll_does_not_leak_hmac_while_seed_is_secret(client):
    """A prova tem de omitir o HMAC enquanto a semente estiver activa."""
    session = guest(client)
    body = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    assert body["proof"]["hmac"] is None
    assert body["proof"]["verifiable"] is False
    assert len(body["proof"]["server_seed_hash"]) == 64


def test_balance_never_goes_negative(client):
    """Nenhuma aposta pode deixar o saldo abaixo de zero.

    Esvazia-se a carteira por abandono, que e deterministico, ao contrario de
    apostar e torcer por uma derrota.
    """
    session = guest(client)

    for _ in range(2):
        duel = client.post(
            "/api/v1/duel/start",
            json={"stake": "5000.00", "player_side": "blue"},
            headers=auth(session),
        ).json()["duel"]
        client.post(f"/api/v1/duel/{duel['id']}/forfeit", headers=auth(session))

    assert dec(client.get("/api/v1/wallet/balance", headers=auth(session)).json()["balance"]) == 0

    response = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "0.10", "mode": "under", "target": "50.00"},
        headers=auth(session),
    )
    assert response.status_code == 422
    assert "insuficiente" in response.json()["detail"]


def test_saldo_nunca_fica_negativo_ao_longo_de_muitas_apostas(client):
    """Invariante verificada a cada aposta, em qualquer desfecho."""
    session = guest(client)

    for _ in range(60):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "100.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )
        balance = client.get("/api/v1/wallet/balance", headers=auth(session)).json()
        assert dec(balance["balance"]) >= 0


def test_bet_above_table_maximum_is_rejected(client):
    session = guest(client)
    response = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "50000.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    )
    assert response.status_code == 422
    assert "maxima" in response.json()["detail"]


def test_bet_below_minimum_is_rejected(client):
    session = guest(client)
    response = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "0.01", "mode": "under", "target": "50.00"},
        headers=auth(session),
    )
    assert response.status_code == 422


def test_target_outside_limits_is_rejected(client):
    session = guest(client)
    for target in ("0.50", "99.00"):
        response = client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "10.00", "mode": "under", "target": target},
            headers=auth(session),
        )
        assert response.status_code == 422, target


def test_invalid_mode_is_rejected(client):
    session = guest(client)
    response = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "10.00", "mode": "sideways", "target": "50.00"},
        headers=auth(session),
    )
    assert response.status_code == 422


def test_nonce_increments_per_roll(client):
    session = guest(client)
    for expected_nonce in range(3):
        body = client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        ).json()
        assert body["proof"]["nonce"] == expected_nonce


# --- historico e estatisticas ---------------------------------------------


def test_history_is_newest_first(client):
    session = guest(client)
    for _ in range(5):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    body = client.get("/api/v1/game/history?limit=5", headers=auth(session)).json()
    nonces = [item["proof"]["nonce"] for item in body["items"]]
    assert nonces == [4, 3, 2, 1, 0]


def test_history_is_scoped_per_user(client):
    first = guest(client)
    client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(first),
    )

    second = guest(client)
    body = client.get("/api/v1/game/history", headers=auth(second)).json()
    assert body["items"] == []


def test_stats_match_the_bets(client):
    session = guest(client)
    for _ in range(10):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "2.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    stats = client.get("/api/v1/stats/me", headers=auth(session)).json()
    assert stats["total_bets"] == 10
    assert stats["wins"] + stats["losses"] == 10
    assert dec(stats["total_wagered"]) == Decimal("20.00")
    assert sum(stats["roll_histogram"].values()) == 10


def test_empty_stats_do_not_blow_up(client):
    session = guest(client)
    stats = client.get("/api/v1/stats/me", headers=auth(session)).json()
    assert stats["total_bets"] == 0
    assert stats["roll_histogram"] == {}


# --- maturidade ------------------------------------------------------------


def test_maturity_starts_on_bronze(client):
    session = guest(client)
    body = client.get("/api/v1/wallet/maturity", headers=auth(session)).json()
    assert body["tier"]["name"] == "Bronze"
    assert body["tier"]["rakeback"] == 0
    assert body["next_tier"]["name"] == "Prata"


def test_wagering_advances_progress(client):
    session = guest(client)
    for _ in range(20):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "100.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    body = client.get("/api/v1/wallet/maturity", headers=auth(session)).json()
    assert dec(body["wagered_total"]) == Decimal("2000.00")
    assert 0 < body["progress"] <= 1


def test_odds_do_not_change_with_maturity(client):
    """O nivel pode subir, mas as odds da mesa tem de ser identicas."""
    session = guest(client)
    first = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    for _ in range(20):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1000.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    last = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    assert dec(first["multiplier"]) == dec(last["multiplier"]) == Decimal("1.98")
    assert dec(first["chance"]) == dec(last["chance"]) == Decimal("0.5")
    # 1,00 (first) + 20 x 1000,00 + 1,00 (last)
    assert dec(last["maturity"]["wagered_total"]) == Decimal("20002.00")


# --- verificacao -----------------------------------------------------------


def test_current_fairness_publishes_hash(client):
    session = guest(client)
    body = client.get("/api/v1/fairness/current", headers=auth(session)).json()
    assert len(body["server_seed_hash"]) == 64
    assert body["client_seed"] == session["user"]["client_seed"]
    assert body["revealed"] is False


def test_rotation_reveals_seed_and_makes_history_verifiable(client):
    session = guest(client)
    bet = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    rotation = client.post("/api/v1/fairness/seed/rotate", headers=auth(session)).json()
    assert rotation["rotated"] is True
    assert rotation["server_seed"]
    assert rotation["server_seed_hash"] == hash_server_seed(rotation["server_seed"])

    history = client.get("/api/v1/game/history", headers=auth(session)).json()
    old = next(item for item in history["items"] if item["id"] == bet["id"])
    assert old["proof"]["verifiable"] is True
    assert old["proof"]["hmac"] is not None


def test_revealed_seed_reproduces_the_exact_roll(client):
    """A garantia central: semente revelada + nonce reproduzem o resultado."""
    session = guest(client)
    bets = [
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        ).json()
        for _ in range(15)
    ]

    rotation = client.post("/api/v1/fairness/seed/rotate", headers=auth(session)).json()
    server_seed = rotation["server_seed"]
    client_seed = session["user"]["client_seed"]

    for bet in bets:
        nonce = bet["proof"]["nonce"]
        expected = compute_roll(server_seed, client_seed, nonce)
        assert expected == dec(bet["roll"]), (
            f"nonce {nonce}: servidor {bet['roll']}, recalculado {expected}"
        )


def test_verify_endpoint_accepts_a_valid_seed(client):
    session = guest(client)
    bet = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    rotation = client.post("/api/v1/fairness/seed/rotate", headers=auth(session)).json()

    body = client.post(
        "/api/v1/fairness/verify",
        json={
            "server_seed": rotation["server_seed"],
            "server_seed_hash": bet["proof"]["server_seed_hash"],
            "client_seed": session["user"]["client_seed"],
            "nonce": bet["proof"]["nonce"],
        },
    ).json()

    assert body["hash_matches"] is True
    assert dec(body["roll"]) == dec(bet["roll"])


def test_verify_endpoint_detects_a_tampered_seed(client):
    session = guest(client)
    bet = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()

    response = client.post(
        "/api/v1/fairness/verify",
        json={
            "server_seed": "00" * 32,
            "server_seed_hash": bet["proof"]["server_seed_hash"],
            "client_seed": session["user"]["client_seed"],
            "nonce": bet["proof"]["nonce"],
        },
    ).json()

    assert response["hash_matches"] is False


def test_client_seed_change_does_not_reset_nonce(client):
    session = guest(client)
    for _ in range(3):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    changed = client.post(
        "/api/v1/fairness/client-seed",
        json={"client_seed": "a-mensagem-do-jogador"},
        headers=auth(session),
    ).json()

    assert changed["client_seed"] == "a-mensagem-do-jogador"
    assert changed["nonce"] == 3

    body = client.post(
        "/api/v1/game/roll",
        json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
        headers=auth(session),
    ).json()
    assert body["proof"]["nonce"] == 3
    assert body["proof"]["client_seed"] == "a-mensagem-do-jogador"


# --- transacoes ------------------------------------------------------------


def test_ledger_records_every_bet(client):
    session = guest(client)
    for _ in range(4):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    entries = client.get(
        "/api/v1/wallet/transactions", headers=auth(session)
    ).json()

    kinds = [e["kind"] for e in entries]
    assert kinds.count("bet_win") + kinds.count("bet_loss") == 4
    assert "bonus" in kinds


def test_ledger_chain_is_internally_consistent(client):
    """Cada saldo tem de ser o saldo anterior mais o montante da entrada.

    E o que garante que o historico de transaccoes conta a mesma historia que
    o saldo actual. Se um dia esta cadeia partir, ha saldo a "nascer do nada".
    """
    session = guest(client)
    for _ in range(12):
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1.00", "mode": "under", "target": "50.00"},
            headers=auth(session),
        )

    entries = client.get(
        "/api/v1/wallet/transactions?limit=200", headers=auth(session)
    ).json()

    # A API devolve do mais recente para o mais antigo.
    ordered = list(reversed(entries))
    assert ordered[0]["kind"] == "bonus"
    assert dec(ordered[0]["balance_after"]) == dec(ordered[0]["amount"])

    for previous, current in zip(ordered, ordered[1:]):
        expected = dec(previous["balance_after"]) + dec(current["amount"])
        assert dec(current["balance_after"]) == expected, current["kind"]

    final = client.get("/api/v1/wallet/balance", headers=auth(session)).json()
    assert dec(final["balance"]) == dec(ordered[-1]["balance_after"])
