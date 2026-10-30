from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Colunas que foram acrescentadas a tabelas que ja existem em bases criadas por
# versoes anteriores. O `create_all` cria tabelas em falta mas nunca acrescenta
# colunas a uma tabela existente, por isso um `duel_id` novo numa base velha
#(resultado de `no such column: ledger_entries.duel_id` ao criar uma entrada).
#
# Cada entrada e (tabela, coluna, DDL). E aditiva e idempotente: correr duas
# vezes nao faz nada na segunda.
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    (
        "ledger_entries",
        "duel_id",
        "INTEGER REFERENCES duels (id)",
    ),
]


def _apply_additive_columns() -> None:
    """Acrescenta colunas em falta a tabelas ja existentes. so para SQLite."""
    if not settings.database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table, column, ddl in ADDITIVE_COLUMNS:
            if table not in existing_tables:
                # Tabela ainda nao existe: o create_all trata dela.
                continue

            present = {c["name"] for c in inspector.get_columns(table)}
            if column in present:
                continue

            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    from app import models  # noqa: F401  (regista as tabelas no metadata)

    Base.metadata.create_all(bind=engine)
    _apply_additive_columns()
