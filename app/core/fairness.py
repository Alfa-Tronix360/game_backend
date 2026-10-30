"""Motor de resultados verificaveis (provably fair) e matematica das odds.

O objectivo e que qualquer jogador possa recalcular, de forma independente,
qualquer resultado ja jogado. O servidor nunca pode mudar o resultado de uma
aposta depois de a aceptar, porque o resultado e funcao apenas de:

    HMAC-SHA256(server_seed, f"{client_seed}:{nonce}")

O `server_seed` e secreto enquanto esta activo. Antes de o usar, o servidor
publica `SHA256(server_seed)`. Como o servidor se compromete com o hash *antes*
de revelar a semente, nao consegue manipular resultados sem deteccao.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

TWO_POW_32 = 2**32
CENTS = Decimal("0.01")

# O multiplicador e guardado com 4 casas, nao 2. Arredondar a 2 casas altera a
# vantagem da casa ate 1,20% nos alvos extremos (over 5 pagava 1,04x em vez de
# 1,0421x), o que quebra a promessa de "1% fixo" publicada na mesa. Com 4 casas
# o desvio fica abaixo de 0,001%.
MULTIPLIER_SCALE = Decimal("0.0001")


class RollMode(str, Enum):
    UNDER = "under"
    OVER = "over"


def generate_server_seed() -> str:
    """Semente secreta de 32 bytes em hexadecimal."""
    return secrets.token_hex(32)


def hash_server_seed(server_seed: str) -> str:
    """Comprometimento publico da semente. Publicado ANTES de a usar."""
    return hashlib.sha256(server_seed.encode("utf-8")).hexdigest()


def float_from_hmac(hmac_hex: str) -> float:
    """Converte um HMAC-SHA256 em hexadecimal num float em [0, 1).

    Percorre o digest de 8 em 8 caracteres hex (32 bits) e devolve o primeiro
    bloco como fracao de 2**32. Um bloco completo de 8 hex tem maximo
    0xFFFFFFFF, que e sempre menor que 2**32, por isso o valor nunca
    atinge 1.0 e o mapeamento para a casa [0, 100) e uniforme.
    """
    for offset in range(0, len(hmac_hex), 8):
        block = hmac_hex[offset : offset + 8]
        if len(block) < 8:
            break
        return int(block, 16) / TWO_POW_32
    return 0.0


def hmac_hexdigest(server_seed: str, client_seed: str, nonce: int) -> str:
    """HMAC-SCA256 em hexadecimal, a partir da semente em hexadecimal.

    ATENCAO: chamar isto exige a semente secreta. So pode ser exposto ao
    jogador depois de a semente ter sido revelada por rotacao.
    """
    message = f"{client_seed}:{nonce}".encode("utf-8")
    return hmac.new(
        key=bytes.fromhex(server_seed),
        msg=message,
        digestmod=hashlib.sha256,
    ).hexdigest()


def compute_roll(server_seed: str, client_seed: str, nonce: int) -> Decimal:
    """Deriva o resultado (0.00 - 99.99) de forma determinista e verificavel."""
    digest = hmac_hexdigest(server_seed, client_seed, nonce)
    value = Decimal(repr(float_from_hmac(digest))) * 100
    return value.quantize(CENTS, rounding=ROUND_DOWN)


def verify_roll(server_seed: str, client_seed: str, nonce: int) -> Decimal:
    """Recalcula um resultado. Usado pelo endpoint de verificacao e pelos testes."""
    return compute_roll(server_seed, client_seed, nonce)


def win_chance(mode: RollMode, target: Decimal) -> Decimal:
    """Probabilidade de vitoria em fraccao (0 < p < 1).

    under: ganha se roll < target  -> p = target / 100
    over:  ganha se roll > target  -> p = (100 - target) / 100
    """
    if mode is RollMode.UNDER:
        return target / Decimal(100)
    return (Decimal(100) - target) / Decimal(100)


def payout_multiplier(mode: RollMode, target: Decimal, house_edge: Decimal) -> Decimal:
    """Multiplicador pago, ja com a vantagem da casa embutida.

    multiplier = (1 - house_edge) / p

    Com target=50 em under e house_edge=0.01 -> 0.99 / 0.50 = 1.98.
    """
    chance = win_chance(mode, target)
    if chance <= 0:
        raise ValueError("A percentagem de vitoria tem de ser maior que zero.")
    return ((Decimal(1) - house_edge) / chance).quantize(
        MULTIPLIER_SCALE, rounding=ROUND_HALF_UP
    )


def expected_value(mode: RollMode, target: Decimal, house_edge: Decimal) -> Decimal:
    """Valor esperado por unidade apostada. Negativo = vantagem da casa.

    Rede de seguranca: em qualquer alvo valido o resultado e -house_edge, ate
    uma fracao de unidade, pelo arredondamento do multiplicador. Se algum
    alvo devolver positivo, a mesa esta a pagar a mais e ha bug.
    """
    multiplier = payout_multiplier(mode, target, house_edge)
    chance = win_chance(mode, target)
    return (multiplier * chance - Decimal(1)).quantize(Decimal("0.000001"))


def is_win(mode: RollMode, roll: Decimal, target: Decimal) -> bool:
    if mode is RollMode.UNDER:
        return roll < target
    return roll > target


# --- dado de seis faces, para o duelo --------------------------------------
#
# Nao se pode simplesmente fazer `int(hash, 16) % 6`: um digest de 256 bits
# deixa 4 restos mais provaveis que os outros dois, porque 2**256 % 6 == 4.
# O viés e de 1 em 2**254, invisivel na pratica, mas nao e zero e um duelo
# justificaria ser exacto.
#
# Aqui le-se o digest em blocos de 32 bits e rejeita-se o intervalo final, o
# unico que nao divide igualmente por 6. A probabilidade de rejeitar e de
# 4 em 2**32, e um unico bloco chega praticamente sempre.
D6_RANGE = TWO_POW_32
D6_BOUNDARY = (D6_RANGE // 6) * 6  # 4294967292
D6_FACES = 6


def d6_from_digest(digest: str) -> int:
    """Face de 1 a 6 a partir de um digest hexadecimal, sem vies."""
    for offset in range(0, len(digest), 8):
        block = digest[offset : offset + 8]
        if len(block) < 8:
            break
        value = int(block, 16)
        if value < D6_BOUNDARY:
            return value % D6_FACES + 1
    # So acontece se o digest for absurdamente curto. Repete com o bloco cheio.
    return int(digest, 16) % D6_FACES + 1


def duel_hmac(server_seed: str, duel_id: int, round_number: int, side: str) -> str:
    """HMAC de uma lançadura do duelo.

    A mensagem inclui id da partida, numero da ronda e lado, de modo que cada
    lançadura e unica e reproduzivel a partir da semente revelada. O prefixo
    `duel:` evita colisao com as lancaduras do dado 0-99, que usam outra
    mensagem.
    """
    message = f"duel:{duel_id}:{round_number}:{side}".encode("utf-8")
    return hmac.new(
        key=bytes.fromhex(server_seed),
        msg=message,
        digestmod=hashlib.sha256,
    ).hexdigest()


def roll_d6(server_seed: str, duel_id: int, round_number: int, side: str) -> int:
    """Face de 1 a 6 para um lado, verificavel fora do servidor."""
    return d6_from_digest(duel_hmac(server_seed, duel_id, round_number, side))


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    blue: int
    red: int
    winner: str  # "blue", "red" ou "draw"
    blue_score: int
    red_score: int


def settle_round(
    *,
    server_seed: str,
    duel_id: int,
    round_number: int,
    blue_score: int,
    red_score: int,
) -> RoundOutcome:
    """Lanca os dois dados de uma vez e aplica a regra da ronda.

    Os dois dados saem sempre da mesma semente e do mesmo instante, por isso
    nenhum lado pode ser favorecido pela ordem de chamada. O bot vermelho nao
    tem advantage nenhuma sobre o jogador.
    """
    blue = roll_d6(server_seed, duel_id, round_number, "blue")
    red = roll_d6(server_seed, duel_id, round_number, "red")

    if blue > red:
        return RoundOutcome(blue, red, "blue", blue_score + 1, red_score)
    if red > blue:
        return RoundOutcome(blue, red, "red", blue_score, red_score + 1)
    return RoundOutcome(blue, red, "draw", blue_score, red_score)


@dataclass(frozen=True, slots=True)
class DiceOutcome:
    roll: Decimal
    won: bool
    multiplier: Decimal
    payout: Decimal
    profit: Decimal
    chance: Decimal


def settle(
    *,
    mode: RollMode,
    target: Decimal,
    bet: Decimal,
    house_edge: Decimal,
    server_seed: str,
    client_seed: str,
    nonce: int,
) -> DiceOutcome:
    """Executa a aposta: apura o resultado e liquida."""
    roll = compute_roll(server_seed, client_seed, nonce)
    multiplier = payout_multiplier(mode, target, house_edge)
    won = is_win(mode, roll, target)

    if won:
        payout = (bet * multiplier).quantize(CENTS, rounding=ROUND_DOWN)
        profit = (payout - bet).quantize(CENTS)
    else:
        payout = Decimal("0.00")
        profit = -bet

    return DiceOutcome(
        roll=roll,
        won=won,
        multiplier=multiplier,
        payout=payout,
        profit=profit,
        chance=win_chance(mode, target),
    )
