"""Migracao aditiva de esquema.

O `create_all` cria tabelas em falta mas nunca acrescenta colunas a tabelas que
ja existem. Uma base criada antes do duelo tem `ledger_entries` sem a coluna
`duel_id`, e qualquer entrada de duel rebentava com
`no such column: ledger_entries.duel_id`. Estes testes recriam essa situacao.
"""

from sqlalchemy import create_engine, inspect, text

from app.core import database


def _make_legacy_db(path):
    """Cria uma base ao nivel do esquema de antes do duelo.

    `ledger_entries` existe, mas sem `duel_id`, e `duels` nao existe de todo.
    """
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    public_id VARCHAR NOT NULL,
                    balance NUMERIC(18, 2) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE bets (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE ledger_entries (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    kind VARCHAR(24) NOT NULL,
                    amount NUMERIC(18, 2) NOT NULL,
                    balance_after NUMERIC(18, 2) NOT NULL,
                    bet_id INTEGER,
                    note VARCHAR(160),
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, public_id, balance) "
                "VALUES (1, 'abc', 500.00)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ledger_entries "
                "(user_id, kind, amount, balance_after, note, created_at) "
                "VALUES (1, 'bet_win', 10.00, 510.00, 'antigo', '2024-01-01 00:00:00')"
            )
        )
    return engine


def test_init_db_adds_the_missing_duel_column(tmp_path, monkeypatch):
    legacy = _make_legacy_db(tmp_path / "legado.db")
    monkeypatch.setattr(database, "engine", legacy)

    before = {c["name"] for c in inspect(legacy).get_columns("ledger_entries")}
    assert "duel_id" not in before

    database.init_db()

    after = {c["name"] for c in inspect(legacy).get_columns("ledger_entries")}
    assert "duel_id" in after

    # As tabelas novas do duelo tambem tem de ter sido criadas.
    tables = set(inspect(legacy).get_table_names())
    assert "duels" in tables
    assert "duel_rounds" in tables

    # E os dados que la estavam nao podem ter sido perdidos.
    with legacy.connect() as connection:
        rows = connection.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar()
    assert rows == 1


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Correr o arranque duas vezes nao pode partir nada."""
    legacy = _make_legacy_db(tmp_path / "legado2.db")
    monkeypatch.setattr(database, "engine", legacy)

    database.init_db()
    database.init_db()

    with legacy.connect() as connection:
        rows = connection.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar()
    assert rows == 1

    columns = [c["name"] for c in inspect(legacy).get_columns("ledger_entries")]
    assert columns.count("duel_id") == 1


def test_migration_skips_tables_that_do_not_exist_yet(tmp_path, monkeypatch):
    """Numa base completamente nova nao ha nada para alterar."""
    fresh = create_engine(
        f"sqlite:///{tmp_path / 'nova.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", fresh)

    database.init_db()

    tables = set(inspect(fresh).get_table_names())
    assert "ledger_entries" in tables
    assert "duels" in tables
