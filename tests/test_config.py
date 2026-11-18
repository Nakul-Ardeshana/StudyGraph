"""Config helpers and the schema upgrade."""
import app as sg


def test_plain_postgres_urls_use_psycopg3_driver():
    assert sg.normalize_database_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert sg.normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sg.normalize_database_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sg.normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_upload_size_limit_is_set():
    assert sg.app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


def test_safe_redirect_target():
    assert sg.is_safe_redirect_target("/cards?x=1")
    for bad in [None, "", "https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"]:
        assert not sg.is_safe_redirect_target(bad)


def test_old_databases_get_position_column(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy.begin() as conn:
        conn.execute(text("CREATE TABLE topics (id VARCHAR(120) PRIMARY KEY, title VARCHAR(255))"))
    monkeypatch.setattr(sg, "engine", legacy)
    sg.ensure_schema()
    assert "position" in {c["name"] for c in inspect(legacy).get_columns("topics")}
