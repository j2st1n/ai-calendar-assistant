import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
from caldav.lib.error import AuthorizationError, DAVError
from dateutil.parser import parse as parse_date
from icalendar import Calendar, Event

from app.ai.schemas import Recurrence
from app.calendar.recurrence import to_rrule

logger = logging.getLogger(__name__)

class CalendarObjectProtocol(Protocol):
    url: object
    id: object
    data: bytes | str

    def save(self) -> None: ...

    def delete(self) -> None: ...


class CalendarProtocol(Protocol):
    url: object
    name: str

    def objects(self) -> Sequence[CalendarObjectProtocol]: ...

    def save_event(self, ical: str) -> CalendarObjectProtocol: ...


class PrincipalProtocol(Protocol):
    def calendars(self) -> Sequence[CalendarProtocol]: ...


class DAVClientProtocol(Protocol):
    def principal(self) -> PrincipalProtocol: ...

    def get_calendars(self) -> Sequence[CalendarProtocol]: ...

    def calendar(self, url: str) -> CalendarProtocol: ...


class ICalAddable(Protocol):
    def add(self, name: str, value: object) -> None: ...


class DateTimePropertyProtocol(Protocol):
    dt: datetime


class ICalComponentProtocol(Protocol):
    name: str

    def __contains__(self, key: object) -> bool: ...

    def __getitem__(self, key: str) -> DateTimePropertyProtocol: ...

    def __setitem__(self, key: str, value: object) -> None: ...


class ReminderData(TypedDict, total=False):
    minutes_before: int


class EventDataPatch(TypedDict, total=False):
    title: str
    start_time: str
    end_time: str
    timezone: str
    location: str
    description: str


CalDAVResult = dict[str, str]
RecurrenceData = Recurrence | dict[str, object] | None

_DAVClient = cast(Callable[..., DAVClientProtocol], caldav.DAVClient)


class CalDAVServiceError(Exception):
    pass


class CalDAVService:
    async def test_connection(self, url: str, username: str, password: str) -> None:
        try:
            await asyncio.to_thread(self._test_connection_sync, url, username, password)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"连接测试失败：{exc}") from exc

    def _test_connection_sync(self, url: str, username: str, password: str) -> None:
        url = url.strip()
        client = _DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)
        try:
            principal = client.principal()
            if not principal:
                raise CalDAVServiceError("无法获取 CalDAV principal，请检查 URL。")
        except AuthorizationError:
            raise CalDAVServiceError("认证失败，请检查用户名和密码。")
        except DAVError as exc:
            raise CalDAVServiceError(f"连接失败：{exc.reason if getattr(exc, 'reason', None) else exc}")

    async def list_calendars(self, url: str, username: str, password: str) -> list[dict[str, str]]:
        try:
            return await asyncio.to_thread(self._list_calendars_sync, url, username, password)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"拉取日历列表失败：{exc}") from exc

    def _list_calendars_sync(self, url: str, username: str, password: str) -> list[dict[str, str]]:
        url = url.strip()
        client = _DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)
        errors: list[str] = []
        for method in [_try_get_calendars, _try_propfind, _try_principal_calendars]:
            try:
                calendars = method(client, url)
                if calendars:
                    return [{"name": cal.name or str(cal.url), "url": str(cal.url)} for cal in calendars]
            except Exception as exc:
                errors.append(f"{method.__name__}: {exc}")
        error_detail = "; ".join(errors) if errors else "所有方法均未发现日历"
        raise CalDAVServiceError(f"未发现任何日历。({error_detail})")

    async def create_event(self, caldav_url: str, username: str, password: str, calendar_url: str | None,
                           title: str, start_time: str, end_time: str | None, timezone_str: str | None,
                           location: str | None, description: str | None,
                           reminders: Sequence[ReminderData] | None, recurrence: RecurrenceData,
                           is_all_day: bool) -> CalDAVResult:
        try:
            return await asyncio.to_thread(
                self._create_event_sync, caldav_url, username, password, calendar_url,
                title, start_time, end_time, timezone_str, location, description,
                reminders, recurrence, is_all_day)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"创建事件失败：{exc}") from exc

    def _create_event_sync(self, caldav_url: str, username: str, password: str, calendar_url: str | None,
                           title: str, start_time: str, end_time: str | None, timezone_str: str | None,
                           location: str | None, description: str | None,
                           reminders: Sequence[ReminderData] | None, recurrence: RecurrenceData,
                           is_all_day: bool) -> CalDAVResult:
        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                   ssl_verify_cert=False, timeout=120)
        calendars = client.get_calendars()
        target_cal = None
        calendar_url_str = calendar_url.strip() if calendar_url else ""
        for cal in calendars:
            if calendar_url_str and str(cal.url) == calendar_url_str:
                target_cal = cal
                break
        if target_cal is None and calendars:
            target_cal = calendars[0]
        if target_cal is None:
            raise CalDAVServiceError("找不到目标日历。请先在 Console 中拉取并保存日历。")

        uid = str(uuid.uuid4())
        cal = Calendar()
        _ical_add(cal, "prodid", "-//AI Calendar Assistant//EN")
        _ical_add(cal, "version", "2.0")
        event = Event()
        _ical_add(event, "summary", title)
        _ical_add(event, "uid", uid)
        _ical_add(event, "dtstamp", datetime.now(timezone.utc))
        if is_all_day:
            dt = parse_date(start_time)
            _ical_add(event, "dtstart", dt.date())
            if end_time:
                _ical_add(event, "dtend", parse_date(end_time).date())
        else:
            start_dt = _parse_caldav_datetime(start_time, timezone_str)
            end_dt = _parse_caldav_datetime(end_time, timezone_str) if end_time else start_dt + timedelta(hours=1)
            _ical_add(event, "dtstart", start_dt)
            _ical_add(event, "dtend", end_dt)
        if description:
            _ical_add(event, "description", description)
        if location:
            _ical_add(event, "location", location)
        rrule = to_rrule(recurrence) if recurrence else None
        if rrule:
            _ical_add(event, "rrule", rrule)
        from icalendar import Alarm
        for r in (reminders or []):
            alarm = Alarm()
            _ical_add(alarm, "action", "DISPLAY")
            _ical_add(alarm, "trigger", timedelta(minutes=-r.get("minutes_before", 30)))
            _ical_add(alarm, "description", "Reminder")
            event.add_component(alarm)
        cal.add_component(event)
        ical_str = cal.to_ical().decode()
        saved = target_cal.save_event(ical_str)
        href = saved.url or target_cal.url
        return {"uid": uid, "href": str(href)}

    async def delete_event(self, caldav_url: str, username: str, password: str,
                           uid: str | None, href: str | None = None) -> bool:
        try:
            return await asyncio.to_thread(self._delete_event_sync, caldav_url, username, password, uid, href)
        except Exception:
            return False

    async def update_event(self, caldav_url: str, username: str, password: str,
                           event_data: EventDataPatch, uid: str | None = None,
                           href: str | None = None) -> bool:
        try:
            return await asyncio.to_thread(self._update_event_sync, caldav_url, username, password, event_data, uid, href)
        except Exception:
            return False

    def _update_event_sync(self, caldav_url: str, username: str, password: str,
                           event_data: EventDataPatch, uid: str | None,
                           href: str | None) -> bool:
        from icalendar import Calendar as ICal

        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                   ssl_verify_cert=False, timeout=120)
        calendars = client.get_calendars()
        for cal in calendars:
            try:
                for obj in cal.objects():
                    obj_url = str(obj.url)
                    obj_uid = str(obj.id)
                    if (href and obj_url == href) or (uid and obj_uid == uid):
                        ical = ICal.from_ical(obj.data)
                        for component in _ical_components(ical):
                            if component.name == 'VEVENT':
                                title = event_data.get('title')
                                if title:
                                    component['summary'] = title
                                start_time = event_data.get('start_time')
                                timezone_name = event_data.get('timezone', 'Asia/Shanghai')
                                if start_time:
                                    new_start = _parse_caldav_datetime(start_time, timezone_name)
                                    component['dtstart'].dt = new_start
                                    if not event_data.get('end_time') and 'dtend' in component:
                                        component['dtend'].dt = new_start + timedelta(hours=1)
                                end_time = event_data.get('end_time')
                                if end_time:
                                    component['dtend'].dt = _parse_caldav_datetime(end_time, timezone_name)
                                location = event_data.get('location')
                                if location:
                                    component['location'] = location
                                description = event_data.get('description')
                                if description:
                                    component['description'] = description
                        obj.data = ical.to_ical().decode('utf-8')
                        print(f"[caldav update] saving obj at {obj.url}", flush=True)
                        _ = obj.save()
                        print(f"[caldav update] save done", flush=True)
                        return True
            except Exception as exc:
                print(f"[caldav update] error: {exc}", flush=True)
                continue
        print(f"[caldav update] target not found uid={uid} href={href}", flush=True)
        return False

    def _delete_event_sync(self, caldav_url: str, username: str, password: str,
                           uid: str | None, href: str | None) -> bool:
        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                   ssl_verify_cert=False, timeout=120)
        calendars = client.get_calendars()
        for cal in calendars:
            try:
                for obj in cal.objects():
                    obj_url = str(obj.url)
                    obj_uid = str(obj.id)
                    if (href and obj_url == href) or (uid and obj_uid == uid):
                        _ = obj.delete()
                        return True
            except Exception:
                continue
        return False


def _try_get_calendars(client: DAVClientProtocol, url: str) -> Sequence[CalendarProtocol]:
    _ = url
    return client.get_calendars()


def _try_principal_calendars(client: DAVClientProtocol, url: str) -> Sequence[CalendarProtocol]:
    _ = url
    return client.principal().calendars()


def _try_propfind(client: DAVClientProtocol, url: str) -> Sequence[CalendarProtocol]:
    try:
        _ = client.principal()
    except Exception:
        return []
    parts = url.strip("/").split("/")
    cal = client.calendar(url=url)
    cal.name = parts[-1] if parts else url
    return [cal]


def _ical_add(component: object, name: str, value: object) -> None:
    cast(ICalAddable, component).add(name, value)


def _ical_components(calendar: object) -> Sequence[ICalComponentProtocol]:
    from icalendar import Calendar as ICal

    typed_calendar = cast(ICal, calendar)
    walk = cast(Callable[[], Sequence[object]], typed_calendar.walk)
    return cast(Sequence[ICalComponentProtocol], walk())


def _parse_caldav_datetime(value: str, timezone_str: str | None) -> datetime:
    dt = parse_date(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zoneinfo_or_default(timezone_str))
    return dt.astimezone(timezone.utc)


def _zoneinfo_or_default(timezone_str: str | None) -> ZoneInfo:
    name = (timezone_str or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")
