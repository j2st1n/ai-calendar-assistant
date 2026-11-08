import os
from pathlib import Path
import pytest

from app.core.config import BASE_DIR, Settings
from app.core.bootstrap import ensure_data_dir, read_version, read_changes


def test_default_paths_anchored_to_base_dir() -> None:
    settings = Settings()
    expected_data_dir = str((BASE_DIR / "data").resolve())
    expected_db_url = f"sqlite:///{(BASE_DIR / 'data' / 'app.db').resolve()}"

    assert settings.data_dir == expected_data_dir
    assert settings.database_url == expected_db_url


def test_custom_relative_paths_anchored_to_base_dir() -> None:
    custom = Settings(data_dir="custom_data", database_url="sqlite:///custom_data/custom.db")
    expected_data_dir = str((BASE_DIR / "custom_data").resolve())
    expected_db_url = f"sqlite:///{(BASE_DIR / 'custom_data' / 'custom.db').resolve()}"

    assert custom.data_dir == expected_data_dir
    assert custom.database_url == expected_db_url


def test_custom_absolute_paths_preserved(tmp_path: Path) -> None:
    abs_data = tmp_path / "isolated_data"
    abs_db = f"sqlite:///{tmp_path / 'isolated.db'}"

    custom = Settings(data_dir=str(abs_data), database_url=abs_db)

    assert custom.data_dir == str(abs_data)
    assert custom.database_url == abs_db


def test_memory_database_url_preserved() -> None:
    mem = Settings(database_url="sqlite:///:memory:")
    assert mem.database_url == "sqlite:///:memory:"


def test_ensure_data_dir_blocks_workspace_root_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import bootstrap, config

    workspace_root = tmp_path / "workspace"
    app_dir = workspace_root / "apps" / "demo-app"
    app_dir.mkdir(parents=True)
    polluting_dir = workspace_root / "data"

    monkeypatch.setattr(config, "BASE_DIR", app_dir)
    with pytest.raises(RuntimeError, match="Safety guard: data_dir cannot be placed directly at workspace root"):
        bootstrap.ensure_data_dir(polluting_dir)
    assert not polluting_dir.exists()


def test_ensure_data_dir_has_no_guard_outside_workspace_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import bootstrap, config

    app_dir = tmp_path / "standalone" / "demo-app"
    app_dir.mkdir(parents=True)
    target = tmp_path / "standalone" / "data"

    monkeypatch.setattr(config, "BASE_DIR", app_dir)
    bootstrap.ensure_data_dir(target)
    assert target.is_dir()


def test_ensure_data_dir_allows_legitimate_dir(tmp_path: Path) -> None:
    target = tmp_path / "safe_data"
    ensure_data_dir(target)
    assert target.is_dir()


def test_read_version_and_changes_resolve_via_base_dir() -> None:
    version = read_version()
    assert version != ""
    assert version != "dev"

    changes = read_changes()
    assert "version" in changes
    assert "sections" in changes
