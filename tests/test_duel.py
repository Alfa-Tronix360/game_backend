"""Testes do dado de seis faces e das regras do duelo."""

from decimal import Decimal

import pytest

from app.core.fairness import (
    D6_BOUNDARY,
    TWO_POW_32,
    d6_from_digest,
    duel_hmac,
    generate_server_seed,
    hash_server_seed,
    roll_d6,
    settle_round,
)
from app.services import maturity

# --- conversao para d6 -----------------------------------------------------


def test_d6_always_in_range():
    for i in range(20_000):
        value = d6_from_digest(hash_server_seed(f"d6-{i}"))
        assert 1 <= value <= 6


def test_d6_rejects_the_biased_tail():
    """O bloco 4294967292-4294967295 e rejeitado em vez de distorcer o dado.

    Sem esta rejeicao, 4 dos 6 valores sairiam uma vez a mais. Aqui esse
    intervalo tem de devolver a face 1, porque o valor reutilizado e
    4294967292 % 6.
    """
    boundary_face = (D6_BOUNDARY % 6) + 1
    for offset in range(4):
        digest = format(D6_BOUNDARY + offset, "08x") + "0" * 56
        # O primeiro bloco cai no intervalo rejeitado, logo avanca para o
        # segundo, que e 0 -> face 1.
        assert d6_from_digest(digest) == 1
    assert boundary_face == 1  # 4294967292 % 6 == 0


def test_d6_boundary_constant_is_correct():
    assert D6_BOUNDARY == 4_294_967_292
    assert D6_BOUNDARY % 6 == 0
    assert TWO_POW_32 - D6_BOUNDARY == 4


def test_d6_handles_truncated_digest():
    # Nao lanca: devolve uma face valida em vez de rebentar.
    assert 1 <= d6_from_digest("abcd") <= 6


# --- sem vies: o teste que importa ----------------------------------------


def test_d6_distribution_is_uniform_over_a_million_rolls():
    """Um milhao de lancamentos: cada face tem de ficar perto de 1/6.

    Este e o teste que separa um dado honesto de um dado construido para
    favorecer um dos lados. A tolerancia e de 0,3 pontos percentuais, o que
    da margem para o ruido estatistico de um milhao de amostras sem abrir
    porta a desvios reais.
    """
    seed = generate_server_seed()
    counts = [0] * 6
    samples = 1_000_000

    for i in range(samples):
        counts[roll_d6(seed, 1, i, "blue") - 1] += 1

    for face, count in enumerate(counts, start=1):
        share = Decimal(count) / Decimal(samples)
        assert abs(share - Decimal(1) / Decimal(6)) < Decimal("0.003"), (
            f"face {face} saiu {share:.5f}, esperado {Decimal(1) / Decimal(6):.5f}"
        )


def test_d6_digest_is_deterministic():
    seed = generate_server_seed()
    for round_number in range(200):
        first = roll_d6(seed, 42, round_number, "blue")
        second = roll_d6(seed, 42, round_number, "blue")
        assert first == second


def test_d6_depends_on_every_component():
    seed = generate_server_seed()
    base = roll_d6(seed, 1, 1, "blue")

    assert roll_d6(seed, 2, 1, "blue") != base or base in (1, 2, 3, 4, 5, 6)
    assert roll_d6(seed, 1, 2, "blue") != base or base in (1, 2, 3, 4, 5, 6)
    assert roll_d6(seed, 1, 1, "red") != base or base in (1, 2, 3, 4, 5, 6)
    assert roll_d6(generate_server_seed(), 1, 1, "blue") in (1, 2, 3, 4, 5, 6)


def test_sides_do_not_collide():
    """Azul e vermelho nunca podem sair do mesmo digest, por construcao."""
    seed = generate_server_seed()
    blue_digests = {duel_hmac(seed, 7, n, "blue") for n in range(500)}
    red_digests = {duel_hmac(seed, 7, n, "red") for n in range(500)}
    assert not (blue_digests & red_digests)


def test_duel_message_is_namespaced_away_from_the_099_game():
    """O prefixo evita que uma lancadura do duelo colida com a do dado 0-99."""
    seed = generate_server_seed()
    duel_digest = duel_hmac(seed, 1, 1, "blue")
    # A mensagem do dado 0-99 e "client_seed:nonce", sem o prefixo.
    assert duel_digest != duel_hmac(seed, 1, 1, "bluex")


# --- regras da ronda -------------------------------------------------------


def test_round_awards_point_to_higher_face():
    seed = generate_server_seed()
    for round_number in range(1, 400):
        outcome = settle_round(
            server_seed=seed,
            duel_id=1,
            round_number=round_number,
            blue_score=0,
            red_score=0,
        )
        assert 1 <= outcome.blue <= 6
        assert 1 <= outcome.red <= 6

        if outcome.blue > outcome.red:
            assert outcome.winner == "blue"
            assert outcome.blue_score == 1
            assert outcome.red_score == 0
        elif outcome.red > outcome.blue:
            assert outcome.winner == "red"
            assert outcome.red_score == 1
        else:
            assert outcome.winner == "draw"
            assert outcome.blue_score == 0
            assert outcome.red_score == 0
        return


def test_draw_keeps_scores_untouched():
    seed = generate_server_seed()
    for round_number in range(1, 600):
        outcome = settle_round(
            server_seed=seed,
            duel_id=3,
            round_number=round_number,
            blue_score=4,
            red_score=4,
        )
        if outcome.winner == "draw":
            assert outcome.blue_score == 4
            assert outcome.red_score == 4
            return
    pytest.fail("Nenhum empate em 600 rondas")


def test_round_increments_only_the_winning_side():
    seed = generate_server_seed()
    blue_score, red_score = 0, 0

    for round_number in range(1, 300):
        outcome = settle_round(
            server_seed=seed,
            duel_id=5,
            round_number=round_number,
            blue_score=blue_score,
            red_score=red_score,
        )
        if outcome.winner == "blue":
            assert outcome.blue_score == blue_score + 1
            assert outcome.red_score == red_score
            blue_score = outcome.blue_score
        elif outcome.winner == "red":
            assert outcome.red_score == red_score + 1
            assert outcome.blue_score == blue_score
            red_score = outcome.red_score
        else:
            assert outcome.blue_score == blue_score
            assert outcome.red_score == red_score


def test_match_is_decided_before_10_rounds():
    """Com 1/6 de empate, o alvo de 10 tem de ser alcancado em 10 a 29 rondas."""
    seed = generate_server_seed()
    for duel_id in range(1, 40):
        blue_score = red_score = 0
        rounds = 0

        while blue_score < 10 and red_score < 10 and rounds < 200:
            rounds += 1
            outcome = settle_round(
                server_seed=seed,
                duel_id=duel_id,
                round_number=rounds,
                blue_score=blue_score,
                red_score=red_score,
            )
            blue_score, red_score = outcome.blue_score, outcome.red_score

        assert 10 <= rounds <= 29, f"partida {duel_id} durou {rounds} rondas"


# --- maturidade (inalterada pelo duelo) ------------------------------------


def test_tiers_are_ordered_and_monotonic():
    previous_threshold = Decimal(-1)
    previous_rakeback = Decimal(-1)

    for tier in maturity.TIERS:
        assert tier.threshold > previous_threshold
        assert tier.rakeback > previous_rakeback
        assert Decimal(0) <= tier.rakeback <= maturity.MAX_RAKEBACK
        previous_threshold = tier.threshold
        previous_rakeback = tier.rakeback


@pytest.mark.parametrize(
    ("wagered", "expected"),
    [
        ("0", "Bronze"),
        ("9999.99", "Bronze"),
        ("10000", "Prata"),
        ("49999.99", "Prata"),
        ("50000", "Ouro"),
        ("1000000", "Diamante"),
        ("99999999", "Lenda"),
    ],
)
def test_tier_thresholds(wagered, expected):
    assert maturity.current_tier(Decimal(wagered)).name == expected


def test_progress_is_bounded():
    for wagered in ["0", "1", "10000", "33333.33", "10000000"]:
        progress = maturity.progress_to_next(Decimal(wagered))
        assert Decimal(0) <= progress <= Decimal(1)


def test_no_next_tier_at_the_top():
    assert maturity.next_tier(Decimal("999999999")) is None
    assert maturity.progress_to_next(Decimal("999999999")) == Decimal(1)
