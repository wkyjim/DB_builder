from pathlib import Path

from db_builder.env_loader import audit_mode_enabled, external_env_dir, load_external_env


def test_external_env_dir_defaults_outside_project(monkeypatch):
    monkeypatch.delenv("DB_BUILDER_ENV_DIR", raising=False)
    path = external_env_dir()
    assert path.name == "DB_builder_env"
    assert path.parent.name == "Coding"


def test_external_env_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_BUILDER_ENV_DIR", str(tmp_path))
    assert external_env_dir() == tmp_path


def test_external_env_preserves_process_values(monkeypatch, tmp_path):
    env_file = tmp_path / "service" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("EXTERNAL_TEST_VALUE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DB_BUILDER_ENV_DIR", str(tmp_path))
    monkeypatch.setenv("EXTERNAL_TEST_VALUE", "from-process")

    loaded = load_external_env(Path("service") / ".env")

    assert loaded == env_file
    assert __import__("os").environ["EXTERNAL_TEST_VALUE"] == "from-process"


def test_audit_mode_does_not_load_external_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AUDIT_SECRET_SENTINEL=must-not-load\n", encoding="utf-8")
    monkeypatch.setenv("DB_BUILDER_ENV_DIR", str(tmp_path))
    monkeypatch.setenv("DB_BUILDER_AUDIT_MODE", "1")
    monkeypatch.delenv("AUDIT_SECRET_SENTINEL", raising=False)

    load_external_env()

    assert audit_mode_enabled() is True
    assert "AUDIT_SECRET_SENTINEL" not in __import__("os").environ
