from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "E+Luck API"
    debug: bool = True

    database_url: str = "sqlite:///./eplusluck.db"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 30

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Moedas fictícias concedidas ao abrir uma sessão nova.
    starting_balance: Decimal = Decimal("10000.00")

    # Vantagem da casa: 0.01 = 1%. Fixa e publica. Nunca muda em runtime,
    # caso contrario a garantia de provable fairness deixa de valer.
    house_edge: Decimal = Decimal("0.01")

    # Limites da mesa, em percentagem de vitoria.
    min_chance: Decimal = Decimal("2.00")
    max_chance: Decimal = Decimal("98.00")

    min_bet: Decimal = Decimal("0.10")
    max_bet: Decimal = Decimal("10000.00")

    # Pontos de ronda necessarios para ganhar o duelo.
    duel_target_score: int = 10
    duel_max_stake: Decimal = Decimal("5000.00")

    history_default_limit: int = 50
    history_max_limit: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
