"""Testes do motor de resultados e da matematica das odds."""

from decimal import Decimal

import pytest

from app.core.fairness import (
    TWO_POW_32,
    DiceOutcome,
    RollMode,
    compute_roll,
    expected_value,
    float_from_hmac,
    generate_server_seed,
    hash_server_seed,
    is_win,
    payout_multiplier,
    settle,
    verify_roll,
    win_chance,
    hmac_hexdigest,
)

EDGE = Decimal("0.01")


# --- conversao hmac -> float ------------------------------------------------


def test_float_from_hmac_within_unit_interval():
    for i in range(2000):
        digest = hash_server_seed(f"seed-{i}")
        value = float_from_hmac(digest)
        assert 0.0 <= value < 1.0


def test_float_from_hmac_never_reaches_one():
    """O bloco de 8 hex tem maximo 0xFFFFFFFF, logo o topo e exclusivo."""
    assert float_from_hmac("ffffffff") == (TWO_POW_32 - 1) / TWO_POW_32
    assert float_from_hmac("ffffffff") < 1.0


def test_float_from_hmac_truncated_input_is_zero():
    assert float_from_hmac("abc") == 0.0


# --- determinismo ----------------------------------------------------------


def test_roll_is_deterministic():
    seed = generate_server_seed()
    a = compute_roll(seed, "cliente", 42)
    b = compute_roll(seed, "cliente", 42)
    assert a == b


def test_different_nonce_gives_different_roll():
    seed = generate_server_seed()
    rolls = {compute_roll(seed, "cliente", n) for n in range(50)}
    assert len(rolls) > 45


def test_different_client_seed_changes_roll():
    seed = generate_server_seed()
    assert compute_roll(seed, "a", 1) != compute_roll(seed, "b", 1)


def test_verify_matches_compute():
    seed = generate_server_seed()
    for nonce in range(25):
        assert verify_roll(seed, "seed-de-teste", nonce) == compute_roll(
            seed, "seed-de-teste", nonce
        )


def test_roll_always_two_decimals():
    seed = generate_server_seed()
    for nonce in range(500):
        roll = compute_roll(seed, "x", nonce)
        assert roll == roll.quantize(Decimal("0.01"))
        assert Decimal("0.00") <= roll < Decimal("100.00")


# --- compromisso da semente -------------------------------------------------


def test_server_seed_hash_is_sha256_of_seed():
    seed = generate_server_seed()
    assert hash_server_seed(seed) == hash_server_seed(seed)
    assert len(hash_server_seed(seed)) == 64
    assert hash_server_seed(seed) != hash_server_seed(generate_server_seed())


def test_server_seed_is_hex_secret():
    seed = generate_server_seed()
    assert len(seed) == 64
    bytes.fromhex(seed)  # lanca se nao for hexadecimal valido


def test_hmac_hexdigest_shape():
    seed = generate_server_seed()
    digest = hmac_hexdigest(seed, "c", 1)
    assert len(digest) == 64
    int(digest, 16)  # lanca se nao for hex


# --- matematica das odds ---------------------------------------------------


def test_win_chance_under_and_over():
    assert win_chance(RollMode.UNDER, Decimal("50")) == Decimal("0.5")
    assert win_chance(RollMode.OVER, Decimal("50")) == Decimal("0.5")
    assert win_chance(RollMode.OVER, Decimal("90")) == Decimal("0.1")
    assert win_chance(RollMode.UNDER, Decimal("10")) == Decimal("0.1")


def test_multiplier_at_50_percent():
    """Alvo classico: 50% de chance, 1.98x com 1% de vantagem da casa."""
    assert payout_multiplier(RollMode.UNDER, Decimal("50.00"), EDGE) == Decimal("1.98")


def test_multiplier_scales_with_risk():
    easy = payout_multiplier(RollMode.UNDER, Decimal("90.00"), EDGE)
    hard = payout_multiplier(RollMode.UNDER, Decimal("10.00"), EDGE)
    assert easy == Decimal("1.10")
    assert hard == Decimal("9.90")
    assert hard > easy


def test_multiplier_rejects_zero_chance():
    with pytest.raises(ValueError):
        payout_multiplier(RollMode.OVER, Decimal("100.00"), EDGE)


def test_is_win_respects_mode():
    roll = Decimal("45.00")
    assert is_win(RollMode.UNDER, roll, Decimal("50.00")) is True
    assert is_win(RollMode.OVER, roll, Decimal("50.00")) is False
    # Empate com o alvo conta como derrota em ambos os sentidos.
    assert is_win(RollMode.UNDER, roll, Decimal("45.00")) is False
    assert is_win(RollMode.OVER, roll, Decimal("45.00")) is False


# --- valor esperado: a garantia mais importante ----------------------------


@pytest.mark.parametrize("target", ["2", "5", "25", "50", "75", "95", "98"])
@pytest.mark.parametrize("mode", [RollMode.UNDER, RollMode.OVER])
def test_expected_value_is_always_minus_house_edge(mode, target):
    """O valor esperado tem de ser -house_edge, em qualquer alvo.

    Tolerancia de 0,0001: vem do arredondamento do multiplicador a 4 casas, nao
    do desenho da mesa. Se algum alvo devolver mais, a mesa esta a pagar a mais
    e ha bug.
    """
    ev = expected_value(mode, Decimal(target), EDGE)
    assert abs(ev + EDGE) < Decimal("0.0001"), f"{mode} {target} -> {ev}"
    assert ev < 0


def test_multiplier_keeps_four_decimal_precision():
    """Alvos extremos exigem mais que 2 casas para a vantagem ficar em 1%."""
    assert payout_multiplier(RollMode.OVER, Decimal("5.00"), EDGE) == Decimal("1.0421")
    assert payout_multiplier(RollMode.UNDER, Decimal("95.00"), EDGE) == Decimal("1.0421")


def test_expected_value_never_positive():
    for step in range(2, 99):
        for mode in RollMode:
            assert expected_value(mode, Decimal(step), EDGE) < 0


# --- liquidacao ------------------------------------------------------------


def test_settle_win_pays_multiplier_minus_stake():
    seed = generate_server_seed()
    bet = Decimal("10.00")
    # procura um nonce que ganhe em under 50
    for nonce in range(500):
        outcome = settle(
            mode=RollMode.UNDER,
            target=Decimal("50.00"),
            bet=bet,
            house_edge=EDGE,
            server_seed=seed,
            client_seed="c",
            nonce=nonce,
        )
        if outcome.won:
            assert outcome.payout == (bet * Decimal("1.98")).quantize(Decimal("0.01"))
            assert outcome.profit == Decimal("9.80")
            assert outcome.chance == Decimal("0.5")
            return
    pytest.fail("Nenhuma vitoria em 500 tentativas")


def test_settle_loss_takes_exactly_the_stake():
    seed = generate_server_seed()
    for nonce in range(500):
        outcome = settle(
            mode=RollMode.UNDER,
            target=Decimal("50.00"),
            bet=Decimal("10.00"),
            house_edge=EDGE,
            server_seed=seed,
            client_seed="c",
            nonce=nonce,
        )
        if not outcome.won:
            assert outcome.payout == Decimal("0.00")
            assert outcome.profit == Decimal("-10.00")
            return
    pytest.fail("Nenhuma derrota em 500 tentativas")


# --- simulacao: a unica prova de que a aleatoriedade e real ----------------


def test_simulation_converges_to_target_probability():
    """Monte Carlo: 200k apostas a 10% de chance devem sair perto de 10%.

    Limite generoso (+/- 0.5 pontos percentuais) porque 200k amostras ainda
    tem ruido. O que se esta a provar e que o gerador nao esta enviesado.
    """
    seed = generate_server_seed()
    samples = 200_000
    target = Decimal("10.00")
    chance = win_chance(RollMode.UNDER, target)

    wins = sum(
        1
        for n in range(samples)
        if is_win(
            RollMode.UNDER, compute_roll(seed, "monte-carlo", n), target
        )
    )
    empirical = Decimal(wins) / Decimal(samples)

    assert abs(empirical - chance) < Decimal("0.005"), (
        f"vistos {empirical:.4f}, esperados {chance}"
    )


def test_roll_distribution_is_uniform_across_deciles():
    """Cada decilo de 0-99 deve receber perto de 10% dos resultados."""
    seed = generate_server_seed()
    samples = 100_000
    counts = [0] * 10

    for n in range(samples):
        roll = compute_roll(seed, "uniformidade", n)
        counts[min(int(roll) // 10, 9)] += 1

    for index, count in enumerate(counts):
        share = Decimal(count) / Decimal(samples)
        assert abs(share - Decimal("0.10")) < Decimal("0.01"), (
            f"decilo {index} recebeu {share:.4f}"
        )


def test_outcome_is_frozen_dataclass():
    outcome = DiceOutcome(
        roll=Decimal("10.00"),
        won=True,
        multiplier=Decimal("1.98"),
        payout=Decimal("19.80"),
        profit=Decimal("9.80"),
        chance=Decimal("0.5"),
    )
    with pytest.raises(Exception):
        outcome.roll = Decimal("20.00")  # type: ignore[misc]
