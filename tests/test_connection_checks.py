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


def test_caldav_save_preserves_check_signature_when_credentials_unchanged():
    from starlette.requests import Request
    from app.core.config import settings
    settings.app_secret_key = "test-secret-key-32-characters-len"
    with session() as db:
        svc = SettingsService(db)
        svc.set('caldav_url', 'https://caldav.example.com')
        svc.set('caldav_username', 'user1')
        svc.set('caldav_password', 'secret', encrypted=True)
        svc.set('caldav_ssl_verify', 'false')
        svc.set('caldav_calendar_url', 'https://caldav.example.com/cal1')
        svc.set('caldav_calendar_name', 'Primary')
        svc.commit()

        # Mark initially verified
        save_check(db, 'caldav', revision(db, 'caldav'), True)
        assert check_summary(db, 'caldav')['label'] == '最近测试成功'

        # Update settings keeping credentials unchanged, but changing calendar name and duration
        req = Request({'type': 'http', 'method': 'POST', 'path': '/console/caldav', 'headers': [(b'accept', b'application/json')], 'session': {}})
        kwargs = dict(
            caldav_url='https://caldav.example.com',
            caldav_username='user1',
            caldav_password='',  # empty keeps saved password
            caldav_calendar_url='https://caldav.example.com/cal2',
            caldav_calendar_name='Secondary',
            caldav_timezone='Asia/Shanghai',
            caldav_reminder_minutes='45',
            caldav_default_duration='90',
            caldav_ssl_verify='false',
            session=db,
            _=None,
        )
        response = asyncio.run(routes.update_caldav_settings(req, **kwargs))
        assert response.status_code == 200
        # Should remain verified and not falsely become "配置已变更"
        assert check_summary(db, 'caldav')['label'] == '最近测试成功'


def test_caldav_test_endpoint_updates_persisted_signature_for_saved_config(monkeypatch):
    from starlette.requests import Request
    from app.core.config import settings
    settings.app_secret_key = "test-secret-key-32-characters-len"
    class Provider:
        async def test_connection(self, *args, **kwargs):
            return True
    monkeypatch.setattr(routes, '_caldav_components', lambda: (Provider, Exception))

    with session() as db:
        svc = SettingsService(db)
        svc.set('caldav_url', 'https://caldav.example.com')
        svc.set('caldav_username', 'user1')
        svc.set('caldav_password', 'secret', encrypted=True)
        svc.set('caldav_ssl_verify', 'false')
        svc.commit()

        assert check_summary(db, 'caldav')['label'] == '未验证'

        req = Request({'type': 'http', 'method': 'POST', 'path': '/console/caldav/test', 'headers': [], 'session': {}})
        response = asyncio.run(routes._probe_caldav(
            db, 'https://caldav.example.com', 'user1', '', 'false', False, request=req
        ))
        assert response.status_code == 200
        # Testing saved configuration successfully should persist check
        assert check_summary(db, 'caldav')['label'] == '最近测试成功'


def test_caldav_test_then_save_syncs_signature_to_new_config(monkeypatch):
    from starlette.requests import Request
    from app.core.config import settings
    settings.app_secret_key = "test-secret-key-32-characters-len"
    class Provider:
        async def test_connection(self, *args, **kwargs):
            return True
    monkeypatch.setattr(routes, '_caldav_components', lambda: (Provider, Exception))

    with session() as db:
        session_store = {}
        req = Request({'type': 'http', 'method': 'POST', 'path': '/console/caldav/test', 'headers': [], 'session': session_store})
        # 1. Test new credentials
        response = asyncio.run(routes._probe_caldav(
            db, 'https://caldav.new.com', 'newuser', 'newpass', 'true', False, request=req
        ))
        assert response.status_code == 200
        assert 'caldav_verified_cred_hash' in session_store

        # 2. Now save the configuration with those tested credentials
        save_req = Request({'type': 'http', 'method': 'POST', 'path': '/console/caldav', 'headers': [(b'accept', b'application/json')], 'session': session_store})
        kwargs = dict(
            caldav_url='https://caldav.new.com',
            caldav_username='newuser',
            caldav_password='newpass',
            caldav_calendar_url='https://caldav.new.com/cal',
            caldav_calendar_name='Work',
            caldav_timezone='Asia/Shanghai',
            caldav_reminder_minutes='30',
            caldav_default_duration='60',
            caldav_ssl_verify='true',
            session=db,
            _=None,
        )
        save_resp = asyncio.run(routes.update_caldav_settings(save_req, **kwargs))
        assert save_resp.status_code == 200
        # The new configuration is automatically validated
        assert check_summary(db, 'caldav')['label'] == '最近测试成功'


def test_caldav_status_context_state_evaluation():
    from app.db.models import EventRecord
    with session() as db:
        svc = SettingsService(db)
        svc.set('caldav_url', 'https://caldav.example.com')
        svc.set('caldav_calendar_name', 'Work')
        svc.set('caldav_calendar_url', 'https://caldav.example.com/cal')
        svc.commit()

        # 1. Valid check older than 24 hours without config change maintains dot-success (已验证), not 需复核
        svc.set('connection_check_caldav', json.dumps({
            'revision': revision(db, 'caldav'),
            'ok': True,
            'at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        }))
        svc.commit()

        ctx = routes.status_context(db)
        services = {s['id']: s for s in ctx['services_status']}
        assert services['caldav']['dot_class'] == 'dot-success'
        assert services['caldav']['state_label'] == '已验证'

        # 2. Config actually changed without re-test -> dot-warning (需复核)
        svc.set('caldav_url', 'https://caldav.changed.com')
        svc.commit()
        ctx = routes.status_context(db)
        services = {s['id']: s for s in ctx['services_status']}
        assert services['caldav']['dot_class'] == 'dot-warning'
        assert services['caldav']['state_label'] == '需复核'

        # 3. With business success under current config version -> dot-success (已连接)
        ver = svc.get_config_version()
        rec = EventRecord(
            status='success',
            operation='create',
            config_version=ver,
            created_at=datetime.now(timezone.utc),
        )
        db.add(rec)
        db.commit()

        ctx = routes.status_context(db)
        services = {s['id']: s for s in ctx['services_status']}
        assert services['caldav']['dot_class'] == 'dot-success'
        assert services['caldav']['state_label'] == '已连接'
