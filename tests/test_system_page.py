import io
import json
from pathlib import Path
import sqlite3
import zipfile

from app.web.routes import _create_backup_archive


def test_system_page_mentions_manual_backup_for_custom_paths():
    template = Path("app/web/templates/system.html").read_text()

    assert "若你自定义了数据目录或数据库路径，请手动备份对应的 app.db 和 secrets.json。" in template


def test_security_is_embedded_and_uses_masked_in_app_modals():
    system_template = Path("app/web/templates/system.html").read_text()
    security_template = Path("app/web/templates/_security_section.html").read_text()
    base_template = Path("app/web/templates/base.html").read_text()

    assert '{% include "_security_section.html" %}' in system_template
    assert 'href="/console/security"' not in base_template
    assert "prompt(" not in security_template
    assert 'name="current_password" type="password"' in security_template
    assert "/console/security/totp/setup" in security_template
    assert 'id="totp-qr-image"' in security_template
    assert 'id="passkey-add-modal"' in security_template
    assert 'id="passkey-delete-modal"' in security_template
    assert '<input name="clear_secret"' not in security_template
    assert 'type="submit" name="clear_secret" value="true" class="danger"' in security_template


def test_login_page_does_not_prefill_admin_username():
    template = Path("app/web/templates/login.html").read_text()

    assert 'name="username" autocomplete="username" value="admin"' not in template
    assert 'name="username" autocomplete="username" required' in template


def test_mobile_navigation_uses_a_scrollable_drawer():
    template = Path("app/web/templates/base.html").read_text()
    styles = Path("app/web/static/styles.css").read_text()

    assert 'href="/static/styles.css?v=4"' in template
    assert 'data-mobile-menu-toggle' in template
    assert 'id="console-sidebar"' in template
    assert 'data-mobile-menu-close' in template
    assert 'data-mobile-menu-backdrop' in template
    assert "event.key === 'Escape'" in template
    assert ".sidebar { display: none; }" not in styles
    assert "body.sidebar-open .sidebar" in styles
    assert "height: 100dvh" in styles
    assert "overflow-y: auto" in styles


def test_base_template_uses_the_standard_favicon_route():
    template = Path("app/web/templates/base.html").read_text()

    assert template.count('rel="icon"') == 1
    assert 'href="/favicon.ico"' in template
    assert "calendar-icon-alt.png" not in template


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
