from gamebridge.database import SCHEMA_VERSION, Database


def test_database_initializes_idempotently(tmp_path):
    database = Database(tmp_path / "gamebridge.db")
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
    assert version == SCHEMA_VERSION
