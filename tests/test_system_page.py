import io
import json
from pathlib import Path
import sqlite3
import zipfile

from app.web.routes import _create_backup_archive


def test_system_page_mentions_manual_backup_for_custom_paths():
    template = Path("app/web/templates/system.html").read_text()

    assert "若你自定义了数据目录或数据库路径，请手动备份对应的 app.db 和 secrets.json。" in template


def test_backup_archive_contains_consistent_restorable_database(tmp_path):
    database_path = tmp_path / "app.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('ok')")
    connection.commit()
    connection.close()
    (tmp_path / "secrets.json").write_text(json.dumps({"token": "encrypted"}))

    backup = _create_backup_archive(tmp_path)

    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        assert set(archive.namelist()) == {"app.db", "secrets.json"}
        restored_path = tmp_path / "restored.db"
        restored_path.write_bytes(archive.read("app.db"))
        assert json.loads(archive.read("secrets.json")) == {"token": "encrypted"}

    restored = sqlite3.connect(restored_path)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM sample").fetchone() == ("ok",)
    finally:
        restored.close()


def test_docker_image_has_healthcheck_and_version_override():
    dockerfile = Path("Dockerfile").read_text()
    compose = Path("docker-compose.yml").read_text()

    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:9527/health" in dockerfile
    assert "${APP_VERSION:-latest}" in compose


def test_docker_workflow_builds_only_version_tags_after_tests():
    workflow = Path(".github/workflows/docker-build.yml").read_text()

    assert "needs: test" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "${{ github.ref_name }}" in workflow
