from pathlib import Path


def test_system_page_mentions_manual_backup_for_custom_paths():
    template = Path("app/web/templates/system.html").read_text()

    assert "若你自定义了数据目录或数据库路径，请手动备份对应的 app.db 和 secrets.json。" in template
