import asyncio
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db.models import Base
from app.services.settings_service import SettingsService
from app.web.connection_checks import revision, save_check, check_summary
from app.web import routes


def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return Session(engine)


def test_check_invalidates_on_configuration_change_but_not_preferences():
    with session() as db:
        svc = SettingsService(db)
        svc.set('ai_model', 'first'); svc.commit()
        assert check_summary(db, 'ai')['label'] == '未验证'
        save_check(db, 'ai', revision(db, 'ai'), True)
        assert check_summary(db, 'ai')['label'] == '最近测试成功'
        svc.set('week_start_day', '0'); svc.commit()
        assert check_summary(db, 'ai')['label'] == '最近测试成功'
        svc.set('ai_model', 'second'); svc.commit()
        assert '配置已变更' in check_summary(db, 'ai')['label']


def test_old_and_malformed_results_are_not_current_success():
    with session() as db:
        svc = SettingsService(db)
        svc.set('connection_check_ai', json.dumps({'at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(), 'ok':True,'revision':revision(db,'ai')})); svc.commit()
        assert '超过 24 小时' in check_summary(db,'ai')['label']
        svc.set('connection_check_ai', '[]'); svc.commit()
        assert check_summary(db,'ai')['label'] == '未验证'


def test_explicit_caldav_failure_is_recorded_without_error_or_password(monkeypatch):
    class Failure(Exception): pass
    class Provider:
        async def test_connection(self, *args, **kwargs):
            raise Failure('private-password internal error')
    monkeypatch.setattr(routes, '_caldav_components', lambda:(Provider,Failure))
    with session() as db:
        svc = SettingsService(db)
        svc.set('caldav_url','https://example.com'); svc.commit()
        response = asyncio.run(routes.test_saved_connection('caldav',db,None))
        assert response.status_code == 200
        assert check_summary(db,'caldav')['label'] == '最近测试失败'
        assert 'private-password' not in response.body.decode()
        assert 'private-password' not in svc.get('connection_check_caldav')


def test_config_changed_while_check_runs_is_stale(monkeypatch):
    class Failure(Exception): pass
    with session() as db:
        svc = SettingsService(db)
        svc.set('caldav_url','https://example.com'); svc.commit()
        class Provider:
            async def test_connection(self, *args, **kwargs):
                svc.set('caldav_url','https://changed.example.com'); svc.commit()
        monkeypatch.setattr(routes,'_caldav_components',lambda:(Provider,Failure))
        response = asyncio.run(routes.test_saved_connection('caldav',db,None))
        assert '配置已变更' in json.loads(response.body)['check']['label']
