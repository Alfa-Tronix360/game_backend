"""Testes de integracao do duelo: ciclo completo de uma partida."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.core.config import settings
from app.main import app

TARGET = settings.duel_target_score


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "teste_duel.db"
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


def dec(value) -> Decimal:
    return Decimal(str(value))


def new_player(client) -> dict:
    session = client.post("/api/v1/auth/guest").json()
    return {"Authorization": f"Bearer {session['token']}"}


def play_to_end(client, auth, max_rounds=60, side="blue", stake="50.00") -> dict:
    """Joga rondas ate a partida terminar e devolve a ultima resposta."""
    start = client.post(
        "/api/v1/duel/start",
        json={"stake": stake, "player_side": side},
        headers=auth,
    )
    assert start.status_code == 201, start.text
    duel_id = start.json()["duel"]["id"]

    response = start.json()
    rounds = 0
    while not response["finished"] and rounds < max_rounds:
        response = client.post(f"/api/v1/duel/{duel_id}/roll", headers=auth).json()
        rounds += 1
    return response


# --- abertura --------------------------------------------------------------


def test_start_debits_the_stake_and_plays_first_round(client):
    auth = new_player(client)
    response = client.post(
        "/api/v1/duel/start",
        json={"stake": "100.00", "player_side": "blue"},
        headers=auth,
    )
    assert response.status_code == 201
    body = response.json()

    assert body["duel"]["stake"] == 100
    assert body["duel"]["round_number"] == 1
    assert body["duel"]["status"] == "active"
    assert 1 <= body["round"]["blue_roll"] <= 6
    assert 1 <= body["round"]["red_roll"] <= 6
    assert dec(body["user"]["balance"]) == dec(
        settings.starting_balance
    ) - Decimal("100.00")


def test_start_publishes_the_seed_hash(client):
    auth = new_player(client)
    body = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    ).json()
    assert len(body["duel"]["server_seed_hash"]) == 64


def test_first_round_can_already_finish_the_match(client):
    """Com alvo 10 isso nao deve acontecer, mas a rota tem de lidar com isso."""
    auth = new_player(client)
    body = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    ).json()
    assert body["finished"] is False
    assert body["duel"]["status"] == "active"


def test_stake_below_minimum_is_rejected(client):
    auth = new_player(client)
    response = client.post(
        "/api/v1/duel/start",
        json={"stake": "0.01", "player_side": "blue"},
        headers=auth,
    )
    assert response.status_code == 422


def test_stake_above_duel_maximum_is_rejected(client):
    auth = new_player(client)
    response = client.post(
        "/api/v1/duel/start",
        json={"stake": "999999.00", "player_side": "blue"},
        headers=auth,
    )
    assert response.status_code == 422


def test_stake_beyond_balance_is_rejected(client):
    auth = new_player(client)

    # Esvazia a carteira em dois passos: uma aposta de 5000 abandonada, e depois
    # outra de 5000 com o que sobrou, tambem abandonada.
    first = client.post(
        "/api/v1/duel/start",
        json={"stake": "5000.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]
    client.post(f"/api/v1/duel/{first['id']}/forfeit", headers=auth)

    second = client.post(
        "/api/v1/duel/start",
        json={"stake": "5000.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]
    client.post(f"/api/v1/duel/{second['id']}/forfeit", headers=auth)

    balance = client.get("/api/v1/wallet/balance", headers=auth).json()
    assert dec(balance["balance"]) == 0

    response = client.post(
        "/api/v1/duel/start",
        json={"stake": "0.10", "player_side": "blue"},
        headers=auth,
    )
    assert response.status_code == 422
    assert "insuficiente" in response.json()["detail"]


def test_only_one_active_match_at_a_time(client):
    auth = new_player(client)
    first = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "red"},
        headers=auth,
    )
    assert second.status_code == 409


def test_invalid_side_is_rejected(client):
    auth = new_player(client)
    response = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "green"},
        headers=auth,
    )
    assert response.status_code == 422


def test_duel_requires_a_session(client):
    response = client.post(
        "/api/v1/duel/start", json={"stake": "10.00", "player_side": "blue"}
    )
    assert response.status_code == 401


# --- regras de pontuacao ---------------------------------------------------


def test_round_awards_the_point_to_the_higher_face(client):
    auth = new_player(client)
    body = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    ).json()

    round_ = body["round"]
    if round_["blue_roll"] > round_["red_roll"]:
        assert round_["winner"] == "blue"
        assert (round_["blue_score"], round_["red_score"]) == (1, 0)
    elif round_["red_roll"] > round_["blue_roll"]:
        assert round_["winner"] == "red"
        assert (round_["blue_score"], round_["red_score"]) == (0, 1)
    else:
        assert round_["winner"] == "draw"
        assert (round_["blue_score"], round_["red_score"]) == (0, 0)


def test_scores_only_ever_grow_by_one_per_round(client):
    auth = new_player(client)
    body = play_to_end(client, auth)
    rounds = body["duel"]["rounds"]

    previous_blue = previous_red = 0
    for round_ in rounds:
        assert round_["blue_score"] in (previous_blue, previous_blue + 1)
        assert round_["red_score"] in (previous_red, previous_red + 1)
        previous_blue, previous_red = round_["blue_score"], round_["red_score"]


def test_match_ends_at_exactly_the_target(client):
    auth = new_player(client)
    body = play_to_end(client, auth)
    duel = body["duel"]

    assert body["finished"] is True
    assert duel["status"] in ("won", "lost")
    assert max(duel["blue_score"], duel["red_score"]) == TARGET
    assert min(duel["blue_score"], duel["red_score"]) <= TARGET - 1
    assert duel["finished_at"] is not None


def test_roll_after_finish_is_refused(client):
    auth = new_player(client)
    body = play_to_end(client, auth)
    duel_id = body["duel"]["id"]

    response = client.post(f"/api/v1/duel/{duel_id}/roll", headers=auth)
    assert response.status_code == 409


# --- liquidacao ------------------------------------------------------------


def test_winning_doubles_the_stake_back(client):
    auth = new_player(client)
    start = settings.starting_balance
    body = play_to_end(client, auth, stake="50.00")
    duel = body["duel"]

    if duel["status"] == "won":
        # Debita-se a aposta e devolve-se o pote (2x). Liquido: +stake.
        assert dec(body["user"]["balance"]) == start + Decimal("50.00")
        assert dec(duel["payout"]) == Decimal("100.00")
    else:
        assert dec(body["user"]["balance"]) == start - Decimal("50.00")
        assert dec(duel["payout"]) == 0


def test_win_and_loss_are_symmetric(client):
    """Quem ganha ganha exactamente a aposta do outro. Sem comissao escondida."""
    auth = new_player(client)
    start = settings.starting_balance
    body = play_to_end(client, auth, stake="40.00")
    duel = body["duel"]
    balance = dec(body["user"]["balance"])

    if duel["status"] == "won":
        assert balance - start == Decimal("40.00")
    else:
        assert start - balance == Decimal("40.00")


def test_playing_as_red_inverts_the_scores(client):
    """Jogar de vermelho nao muda as regras: so muda quem e o jogador."""
    auth = new_player(client)
    body = play_to_end(client, auth, side="red")
    duel = body["duel"]
    assert duel["player_side"] == "red"

    if duel["red_score"] == TARGET:
        assert duel["status"] == "won"
    else:
        assert duel["status"] == "lost"


def test_forfeit_loses_the_stake(client):
    auth = new_player(client)
    start = settings.starting_balance
    duel = client.post(
        "/api/v1/duel/start",
        json={"stake": "75.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]

    response = client.post(f"/api/v1/duel/{duel['id']}/forfeit", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "forfeit"
    assert dec(body["payout"]) == 0

    balance = client.get("/api/v1/wallet/balance", headers=auth).json()
    assert dec(balance["balance"]) == start - Decimal("75.00")


def test_a_new_match_can_start_after_forfeit(client):
    auth = new_player(client)
    duel = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]
    client.post(f"/api/v1/duel/{duel['id']}/forfeit", headers=auth)

    second = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    )
    assert second.status_code == 201


# --- isolamento ------------------------------------------------------------


def test_duels_are_scoped_per_user(client):
    first = new_player(client)
    duel_id = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=first,
    ).json()["duel"]["id"]

    second = new_player(client)
    assert client.get(f"/api/v1/duel/{duel_id}", headers=second).status_code == 404
    assert client.post(f"/api/v1/duel/{duel_id}/roll", headers=second).status_code == 404


def test_unknown_duel_is_404(client):
    auth = new_player(client)
    assert client.get("/api/v1/duel/9999", headers=auth).status_code == 404


def test_history_lists_finished_matches_newest_first(client):
    auth = new_player(client)
    for _ in range(2):
        play_to_end(client, auth)

    body = client.get("/api/v1/duel/list", headers=auth).json()
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] > body["items"][1]["id"]
    assert all(item["status"] != "active" for item in body["items"])


def test_history_hides_active_matches(client):
    auth = new_player(client)
    client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=auth,
    )
    body = client.get("/api/v1/duel/list", headers=auth).json()
    assert body["items"] == []


def test_active_returns_the_open_match_so_it_can_be_resumed(client):
    auth = new_player(client)
    assert client.get("/api/v1/duel/active", headers=auth).json() is None

    start = client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "red"},
        headers=auth,
    ).json()
    active = client.get("/api/v1/duel/active", headers=auth)
    assert active.status_code == 200
    body = active.json()
    assert body["id"] == start["duel"]["id"]
    assert body["status"] == "active"
    assert len(body["rounds"]) == 1

    client.post(f"/api/v1/duel/{body['id']}/forfeit", headers=auth)
    assert client.get("/api/v1/duel/active", headers=auth).json() is None


def test_active_is_private_to_each_player(client):
    owner = new_player(client)
    client.post(
        "/api/v1/duel/start",
        json={"stake": "10.00", "player_side": "blue"},
        headers=owner,
    )
    assert client.get("/api/v1/duel/active", headers=new_player(client)).json() is None


# --- justeza ---------------------------------------------------------------


def test_revealed_seed_reproduces_every_round(client):
    """A garantia central do duelo: semente + ids reproduzem os dois dados."""
    auth = new_player(client)
    body = play_to_end(client, auth)
    duel = body["duel"]

    rotation = client.post("/api/v1/fairness/seed/rotate", headers=auth).json()
    server_seed = rotation["server_seed"]

    for round_ in duel["rounds"]:
        response = client.post(
            "/api/v1/fairness/verify",
            json={
                "server_seed": server_seed,
                "server_seed_hash": duel["server_seed_hash"],
                "client_seed": "",
                "nonce": 0,
            },
        )
        # Endpoint do dado 0-99: so para confirmar que nao interfere.
        assert response.status_code == 200

        blue = client.post(
            "/api/v1/duel/verify",
            json={
                "server_seed": server_seed,
                "server_seed_hash": duel["server_seed_hash"],
                "duel_id": duel["id"],
                "round_number": round_["round_number"],
            },
        ).json()

        assert blue["hash_matches"] is True
        assert f"Azul {round_['blue_roll']}" in blue["message"]
        assert f"vermelho {round_['red_roll']}" in blue["message"]


def test_verify_detects_a_wrong_seed(client):
    auth = new_player(client)
    body = play_to_end(client, auth)
    duel = body["duel"]

    response = client.post(
        "/api/v1/duel/verify",
        json={
            "server_seed": "ab" * 32,
            "server_seed_hash": duel["server_seed_hash"],
            "duel_id": duel["id"],
            "round_number": 1,
        },
    ).json()
    assert response["hash_matches"] is False


def _build_volume(client, auth, target_volume: Decimal) -> Decimal:
    """Faz crescer o volume apostado com apostas de alta probabilidade.

    Apostar 1000 num corte de 98% ganha quase sempre, entao o volume sobe sem
    risco de falhar a meio por falta de saldo.
    """
    for _ in range(60):
        current = dec(
            client.get("/api/v1/wallet/maturity", headers=auth).json()["wagered_total"]
        )
        if current >= target_volume:
            break
        client.post(
            "/api/v1/game/roll",
            json={"bet_amount": "1000.00", "mode": "under", "target": "98.00"},
            headers=auth,
        )
    return dec(client.get("/api/v1/wallet/maturity", headers=auth).json()["wagered_total"])


def test_duel_rules_identical_across_tiers(client):
    """As regras do duelo nao podem depender do nivel da conta.

    Compara-se uma conta nova com uma que ja subiu de nivel, para confirmar
    que faces, alvo e incremento de pontos sao exatamente os mesmos.
    """
    novice_auth = new_player(client)
    novice = play_to_end(client, novice_auth, side="blue", stake="5.00")
    assert (
        client.get("/api/v1/wallet/maturity", headers=novice_auth).json()["tier"]["name"]
        == "Bronze"
    )

    veteran_auth = new_player(client)
    volume = _build_volume(client, veteran_auth, Decimal("10000"))
    assert volume >= Decimal("10000")
    assert (
        client.get("/api/v1/wallet/maturity", headers=veteran_auth).json()["tier"]["name"]
        == "Prata"
    )

    veteran = play_to_end(client, veteran_auth, side="blue", stake="5.00")

    for duel in (novice["duel"], veteran["duel"]):
        assert duel["target_score"] == TARGET
        assert max(duel["blue_score"], duel["red_score"]) == TARGET
        for round_ in duel["rounds"]:
            assert 1 <= round_["blue_roll"] <= 6
            assert 1 <= round_["red_roll"] <= 6


def test_duel_stake_counts_towards_maturity_volume(client):
    auth = new_player(client)
    before = dec(
        client.get("/api/v1/wallet/maturity", headers=auth).json()["wagered_total"]
    )

    duel = client.post(
        "/api/v1/duel/start",
        json={"stake": "250.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]

    after = dec(client.get("/api/v1/wallet/maturity", headers=auth).json()["wagered_total"])
    assert after - before == Decimal(duel["stake"])


# --- regressoes ------------------------------------------------------------


def test_rotating_the_seed_mid_match_keeps_the_original_outcomes(client):
    """A semente fica presa a partida, nao a conta.

    Rodar a semente no meio do duelo nao pode alterar as rondas seguintes. Se
    o lancadouro usasse a semente activa em vez da fixada no arranque, as
    rondas jogadas depois da rotacao seriam impossiveis de provar com a seed
    revelada.
    """
    auth = new_player(client)
    start = client.post(
        "/api/v1/duel/start",
        json={"stake": "50.00", "player_side": "blue"},
        headers=auth,
    ).json()
    duel_id = start["duel"]["id"]
    committed_hash = start["duel"]["server_seed_hash"]

    # A rotacao revela a semente que estava activa, e era a da partida.
    rotated = client.post("/api/v1/fairness/seed/rotate", headers=auth).json()
    assert rotated["rotated"] is True
    revealed_seed = rotated["server_seed"]
    assert rotated["server_seed_hash"] == committed_hash

    # Varias rondas depois da rotacao.
    for _ in range(3):
        rolled = client.post(f"/api/v1/duel/{duel_id}/roll", headers=auth)
        assert rolled.status_code == 200, rolled.text

    duel = client.get(f"/api/v1/duel/{duel_id}", headers=auth).json()

    # O compromisso publicado nunca mudou.
    assert duel["server_seed_hash"] == committed_hash
    assert len(duel["rounds"]) == 4

    # Todas as rondas, antes e depois da rotacao, reproduzem pela seed revelada.
    for round_ in duel["rounds"]:
        proof = client.post(
            "/api/v1/duel/verify",
            json={
                "server_seed": revealed_seed,
                "server_seed_hash": committed_hash,
                "duel_id": duel_id,
                "round_number": round_["round_number"],
            },
        ).json()

        assert proof["hash_matches"] is True
        assert proof["roll"] == round_["blue_roll"], (round_, proof)


def test_a_new_match_uses_the_newly_rotated_seed(client):
    """O contrario do teste anterior: uma partida nova usa a semente nova.

    A rotacao revela a semente anterior e cria outra. A partida aberta depois
    da rotacao tem de ficar presa a semente nova, nao a que acabou de ser
    revelada.
    """
    auth = new_player(client)
    first = client.post(
        "/api/v1/duel/start",
        json={"stake": "50.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]

    # Fecha a primeira partida: so pode haver uma activa de cada vez.
    ended = client.post(f"/api/v1/duel/{first['id']}/forfeit", headers=auth)
    assert ended.status_code == 200, ended.text

    # Revela a semente da primeira partida.
    revealed_first = client.post("/api/v1/fairness/seed/rotate", headers=auth).json()
    seed_a = revealed_first["server_seed"]
    assert revealed_first["server_seed_hash"] == first["server_seed_hash"]

    fresh = client.post(
        "/api/v1/duel/start",
        json={"stake": "50.00", "player_side": "blue"},
        headers=auth,
    ).json()["duel"]

    # A partida nova esta presa a outra semente.
    assert fresh["server_seed_hash"] != first["server_seed_hash"]

    # A seed A nao explica a partida nova.
    for round_ in fresh["rounds"]:
        wrong = client.post(
            "/api/v1/duel/verify",
            json={
                "server_seed": seed_a,
                "server_seed_hash": fresh["server_seed_hash"],
                "duel_id": fresh["id"],
                "round_number": round_["round_number"],
            },
        ).json()
        assert wrong["hash_matches"] is False

    # Rodar de novo revela a semente da partida nova, que a explica.
    revealed_second = client.post("/api/v1/fairness/seed/rotate", headers=auth).json()
    seed_b = revealed_second["server_seed"]
    assert revealed_second["server_seed_hash"] == fresh["server_seed_hash"]

    for round_ in fresh["rounds"]:
        proof = client.post(
            "/api/v1/duel/verify",
            json={
                "server_seed": seed_b,
                "server_seed_hash": fresh["server_seed_hash"],
                "duel_id": fresh["id"],
                "round_number": round_["round_number"],
            },
        ).json()
        assert proof["hash_matches"] is True
        assert proof["roll"] == round_["blue_roll"], (round_, proof)


def test_forfeit_charges_the_stake_only_once(client):
    """A cadeia do ledger tem de fechar com o saldo real.

    A aposta e debitada no arranque. Abandonar nao pode debitar outra vez, senao
    o historico de transaccoes mostra uma perda maior do que a perda verdadeira
    e a soma das entradas deixa de bater certo com o saldo.
    """
    auth = new_player(client)
    opening_balance = dec(
        client.get("/api/v1/wallet/balance", headers=auth).json()["balance"]
    )
    stake = dec("120.00")

    start = client.post(
        "/api/v1/duel/start",
        json={"stake": str(stake), "player_side": "red"},
        headers=auth,
    ).json()
    duel_id = start["duel"]["id"]

    forfeited = client.post(f"/api/v1/duel/{duel_id}/forfeit", headers=auth)
    assert forfeited.status_code == 200, forfeited.text
    assert forfeited.json()["status"] == "forfeit"
    assert dec(forfeited.json()["payout"]) == Decimal("0.00")

    balance = dec(client.get("/api/v1/wallet/balance", headers=auth).json()["balance"])

    # Perdeu-se a aposta, uma unica vez.
    assert balance == opening_balance - stake

    # Nao existe uma segunda debitacao de 120.
    entries = client.get("/api/v1/wallet/transactions?limit=50", headers=auth).json()
    forfeit_entries = [e for e in entries if e["kind"] == "duel_forfeit"]
    assert len(forfeit_entries) == 1
    assert dec(forfeit_entries[0]["amount"]) == Decimal("0.00")

    # E a cadeia de saldos e integra: andando do mais antigo para o mais recente,
    # cada saldo e o anterior mais o valor da entrada.
    ordered = list(reversed(entries))
    running = dec(ordered[0]["balance_after"]) - dec(ordered[0]["amount"])
    for entry in ordered:
        assert dec(entry["balance_after"]) == running + dec(entry["amount"]), entry
        running = dec(entry["balance_after"])

    # O ultimo saldo da cadeia e o saldo real.
    assert running == balance


def client_balance(client, auth) -> str:
    return client.get("/api/v1/wallet/balance", headers=auth).json()["balance"]


def assert_ledger_chain(client, auth) -> None:
    """O saldo tem de sair da soma das entradas, sem sobras.

    Anda do mais antigo para o mais recente: cada `balance_after` e o anterior
    mais o `amount`. No fim o running tem de ser igual ao saldo real. E o que
    garante que nenhuma entrada desloca dinheiro a mais ou a menos.
    """
    balance = dec(client_balance(client, auth))
    entries = client.get("/api/v1/wallet/transactions?limit=200", headers=auth).json()

    ordered = list(reversed(entries))
    running = dec(ordered[0]["balance_after"]) - dec(ordered[0]["amount"])

    for entry in ordered:
        assert dec(entry["balance_after"]) == running + dec(entry["amount"]), entry
        running = dec(entry["balance_after"])

    assert running == balance, (running, balance)


def test_settlement_entries_match_the_pot_and_the_chain_closes(client):
    """Vitoria e derrota, e a cadeia do ledger fecha nas duas.

    A aposta e debitada no arranque, a liquidacao credita o pote. A entrada da
    liquidacao tem de valer o pote: registar `payout - stake` debitava a aposta
    uma segunda vez na derrota e deixava a cadeia a meio caminho na vitoria.

    O desfecho e aleatorio, por isso joga-se ate ter visto os dois. Com 20
    partidas, a probabilidade de sair so um desfecho e de 2^-19, por isso o
    teste falha se acontecer em vez de se limitar a saltar.
    """
    auth = new_player(client)
    stake = dec("20.00")
    running = dec(client_balance(client, auth))

    seen_win = False
    seen_loss = False

    for attempt in range(20):
        result = play_to_end(client, auth, side="blue", stake=str(stake))
        status = result["duel"]["status"]
        balance = dec(result["user"]["balance"])

        if status == "won":
            # Perdeu a aposta e recebeu o dobro: sobra a aposta.
            assert balance == running + stake, (attempt, status)
            seen_win = True
        else:
            # Perdeu a aposta e o pote foi zero: perde so a aposta.
            assert balance == running - stake, (attempt, status)
            seen_loss = True

        running = balance

        entries = client.get("/api/v1/wallet/transactions?limit=200", headers=auth).json()
        settlement = next(
            e for e in entries if e["kind"] in ("duel_win", "duel_loss")
        )
        expected_entry = stake * 2 if status == "won" else Decimal("0.00")
        assert dec(settlement["amount"]) == expected_entry, (attempt, status, settlement)

        assert_ledger_chain(client, auth)

        if seen_win and seen_loss:
            break
    else:
        pytest.fail(
            f"Em 20 partidas so apareceram: vitoria={seen_win}, derrota={seen_loss}. "
            "Um dos desfechos nao foi exercitado."
        )


def test_ledger_chain_holds_across_win_loss_and_forfeit(client):
    """Os tres desfechos na mesma conta: a cadeia fecha sempre."""
    auth = new_player(client)
    running = dec(client_balance(client, auth))

    for index in range(4):
        stake = dec(20 + index)
        result = play_to_end(client, auth, side="blue", stake=str(stake))

        expected = running + stake if result["duel"]["status"] == "won" else running - stake
        assert dec(result["user"]["balance"]) == expected, (index, result["duel"]["status"])

        running = expected
        assert_ledger_chain(client, auth)

    # Uma partida abandonada a meio, para fechar com o terceiro desfecho.
    started = client.post(
        "/api/v1/duel/start",
        json={"stake": "35.00", "player_side": "red"},
        headers=auth,
    ).json()
    client.post(f"/api/v1/duel/{started['duel']['id']}/forfeit", headers=auth)

    assert dec(client_balance(client, auth)) == running - dec("35.00")
    assert_ledger_chain(client, auth)
