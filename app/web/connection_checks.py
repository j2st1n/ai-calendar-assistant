"""Persist only explicit checks of saved configurations; never probe drafts."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import Setting
from app.services.settings_service import SettingsService

FIELDS = {
    'ai': ('ai_provider_name', 'ai_provider_type', 'ai_base_url', 'ai_api_key', 'ai_model'),
    'caldav': ('caldav_url', 'caldav_username', 'caldav_password', 'caldav_ssl_verify', 'caldav_calendar_url', 'caldav_calendar_name'),
}


def revision(session: Session, kind: str) -> str:
    service = SettingsService(session)
    pairs = []
    for k in sorted(FIELDS[kind]):
        val = service.get(k)
        pairs.append((k, val if val is not None else ""))
    data = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def save_check(session: Session, kind: str, signature: str, ok: bool) -> None:
    service = SettingsService(session)
    service.set('connection_check_' + kind, json.dumps({
        'revision': signature, 'ok': ok, 'at': datetime.now(timezone.utc).isoformat(),
    }))
    service.commit()


def check_summary(session: Session, kind: str) -> dict:
    result = {'label': '未验证', 'at': None, 'ok': None, 'reason': 'unverified'}
    try:
        data = json.loads(SettingsService(session).get('connection_check_' + kind) or '{}')
        at = datetime.fromisoformat(data['at'])
        if at.tzinfo is None or not isinstance(data['ok'], bool):
            return result
        result.update(at=at, ok=data['ok'])
        if data['revision'] != revision(session, kind):
            result['label'] = '配置已变更，请重新验证'
            result['reason'] = 'config_changed'
        elif datetime.now(timezone.utc) - at > timedelta(hours=24):
            result['label'] = '测试已超过 24 小时，请重新验证'
            result['reason'] = 'expired_24h'
        else:
            result['label'] = '最近测试成功' if data['ok'] else '最近测试失败'
            result['reason'] = 'recent_success' if data['ok'] else 'recent_failure'
    except (ValueError, KeyError, TypeError):
        pass
    return result
