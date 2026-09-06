import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db.models import Base, EventRecord
from app.web.event_presenter import event_feedback
from app.web.routes import event_records


def record(operation='no_event', error='', payload=None, status='failed'):
    return EventRecord(operation=operation, status=status, error_message=error,
                       event_json=json.dumps(payload) if payload is not None else None)


@pytest.mark.parametrize('error,label', [
    ('AI 调用失败：429 Monthly usage limit reached', 'AI 额度不足'),
    ('AI 调用失败：Error code: 429', 'AI 请求受限'),
    ('AI 调用失败：Error code: 401', 'AI 访问受限'),
    ('AI 调用失败：Connection timed out', 'AI 连接异常'),
    ('invalid output', 'AI 处理失败'),
])
def test_legacy_system_errors_are_not_classified_as_no_event(error, label):
    rec = record(error=error, payload={'intent': 'no_event', 'error_type': 'system_error', 'missing_fields': [error]})
    assert event_feedback(rec)['result_label'] == label
    assert event_feedback(rec)['action_url'] == '/console/ai'


def test_caldav_429_is_not_ai_quota_error():
    result = event_feedback(record('create', '429 quota exceeded'))
    assert result['result_label'] == '日历操作失败'
    assert result['action_url'] == '/console/caldav'
    assert '核对' in result['suggestion']


@pytest.mark.parametrize('payload,error,label', [
    ({'missing_fields': ['时间']}, '缺少字段：时间', '待补充信息'),
    ({'intent': 'no_event'}, '未识别到日程信息', '未识别到日程'),
    ({'unsupported_reason': 'recurrence'}, '', '暂不支持'),
    (['unexpected'], '', '处理失败'),
])
def test_business_and_unknown_results_remain_distinct(payload,error,label):
    assert event_feedback(record(payload=payload, error=error))['result_label'] == label


def test_corrupt_legacy_json_and_success_records_are_safe():
    rec = record(); rec.event_json = '{broken'
    assert event_feedback(rec)['result_label'] == '处理失败'
    rec.status = 'success'
    assert event_feedback(rec)['reason'] == ''


def test_filtered_event_page_escapes_content_and_keeps_read_only_details():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            EventRecord(operation='no_event',status='failed',original_text='<script>alert(1)</script>测试',error_message='AI 调用失败：429'),
            EventRecord(operation='create',status='success',title='测试成功记录'),
        ])
        session.commit()
        request = Request({'type':'http','method':'GET','path':'/console/events','headers':[], 'query_string':b'', 'session':{'admin_authenticated':True}})
        response = asyncio.run(event_records(request,status_filter='failed',search=' 测试 ',session=session,_=None))
        body=response.body.decode()
        assert 'AI 请求受限' in body
        assert '测试成功记录' not in body
        assert '&lt;script&gt;' in body and '<script>alert(1)</script>' not in body
        assert '<option value="failed" selected>失败</option>' in body
        assert '清除筛选' in body and '技术详情' in body
        assert '<details class="event-record">' in body
        response=asyncio.run(event_records(request,status_filter='invalid',search='不存在',session=session,_=None))
        assert '没有匹配的记录' in response.body.decode()


def test_pagination_retains_combined_filters_and_local_date_boundaries():
    from datetime import datetime
    from app.services.settings_service import SettingsService
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        service = SettingsService(session)
        service.set('caldav_timezone', 'Asia/Shanghai')
        for i in range(101):
            session.add(EventRecord(title=f'匹配 {i}', source='wechat', status='failed', operation='create', created_at=datetime(2026, 9, 5, 16, 0)))
        session.add(EventRecord(operation='create', title='前一天', source='wechat', status='failed', created_at=datetime(2026, 9, 5, 15, 59)))
        session.add(EventRecord(operation='create', title='其他渠道', source='telegram', status='failed', created_at=datetime(2026, 9, 5, 16, 0)))
        session.commit()
        request = Request({'type':'http','method':'GET','path':'/console/events','headers':[], 'query_string':b'', 'session':{'admin_authenticated':True}})
        args = dict(request=request, session=session, _=None, status_filter='failed', source='wechat', date_from='2026-09-06', date_to='2026-09-06')
        response = asyncio.run(event_records(**args, page=5))
        assert response.context['total'] == 101
        assert len(response.context['events']) == 1
        assert response.context['events'][0]['title'] == '匹配 0'
        assert 'source=wechat' in response.context['previous_url']
        assert 'date_from=2026-09-06' in response.context['previous_url']
        assert asyncio.run(event_records(**args, page=999)).context['page'] == 5
        args['date_from'] = '2026-09-07'
        response = asyncio.run(event_records(**args))
        assert response.context['total'] == 0
        assert response.context['filter_error']
