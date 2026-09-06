import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypedDict, cast
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

def _DAVClient(**kwargs: object) -> DAVClientProtocol:
    import caldav

    factory = cast(Callable[..., DAVClientProtocol], caldav.DAVClient)
    return factory(**kwargs)


def _caldav_errors() -> tuple[type[Exception], type[Exception]]:
    from caldav.lib.error import AuthorizationError, DAVError

    return AuthorizationError, DAVError


class CalDAVServiceError(Exception):
    pass


class CalDAVService:
    async def test_connection(self, url: str, username: str, password: str, ssl_verify: bool = True) -> None:
        try:
            await asyncio.to_thread(self._test_connection_sync, url, username, password, ssl_verify)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"连接测试失败：{exc}") from exc

    def _test_connection_sync(self, url: str, username: str, password: str, ssl_verify: bool) -> None:
        url = url.strip()
        client = _DAVClient(url=url, username=username, password=password, ssl_verify_cert=ssl_verify, timeout=120)
        AuthorizationError, DAVError = _caldav_errors()
        try:
            principal = client.principal()
            if not principal:
                raise CalDAVServiceError("无法获取 CalDAV principal，请检查 URL。")
        except AuthorizationError:
            raise CalDAVServiceError("认证失败，请检查用户名和密码。")
        except DAVError as exc:
            reason = getattr(exc, "reason", None)
            raise CalDAVServiceError(f"连接失败：{reason or exc}")

    async def list_calendars(self, url: str, username: str, password: str, ssl_verify: bool = True) -> list[dict[str, str]]:
        try:
            return await asyncio.to_thread(self._list_calendars_sync, url, username, password, ssl_verify)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"拉取日历列表失败：{exc}") from exc

    def _list_calendars_sync(self, url: str, username: str, password: str, ssl_verify: bool) -> list[dict[str, str]]:
        url = url.strip()
        candidate_urls = _get_discovery_urls(url)
        errors: list[str] = []

        # 1. 优先尝试主流程：_try_get_calendars 与 _try_principal_calendars
        #    对于包含日历后缀的 URL，candidate_urls 已优先向上包含主体/集合路径，确保完整拉取全部日历
        for cand_url in candidate_urls:
            client = _DAVClient(url=cand_url, username=username, password=password, ssl_verify_cert=ssl_verify, timeout=120)
            for method in [_try_get_calendars, _try_principal_calendars]:
                try:
                    calendars = method(client, cand_url)
                    if calendars:
                        return [{"name": _get_calendar_name(cal), "url": str(cal.url)} for cal in calendars]
                except Exception as exc:
                    errors.append(f"{method.__name__} ({cand_url}): {exc}")

        # 2. 单日历兜底移至最后（主要针对 163/QQ 邮箱等无标准主体的服务）
        original_client = _DAVClient(url=url, username=username, password=password, ssl_verify_cert=ssl_verify, timeout=120)
        try:
            calendars = _try_propfind(original_client, url)
            if calendars:
                return [{"name": _get_calendar_name(cal), "url": str(cal.url)} for cal in calendars]
        except Exception as exc:
            errors.append(f"_try_propfind: {exc}")

        error_detail = "; ".join(errors) if errors else "所有方法均未发现日历"
        raise CalDAVServiceError(f"未发现任何日历。({error_detail})")

    async def probe_write(
        self,
        url: str,
        username: str,
        password: str,
        calendar_url: str | None = None,
        ssl_verify: bool = True,
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._probe_write_sync, url, username, password, calendar_url, ssl_verify
            )
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"日历写入探针失败：{exc}") from exc

    def _probe_write_sync(
        self,
        caldav_url: str,
        username: str,
        password: str,
        calendar_url: str | None,
        ssl_verify: bool,
    ) -> dict[str, Any]:
        from icalendar import Calendar, Event

        probe_uid = f"probe-{uuid.uuid4()}"
        probe_summary = f"[PROBE-TEST] 权限验证-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)

        client = _DAVClient(
            url=caldav_url.strip(),
            username=username,
            password=password,
            ssl_verify_cert=ssl_verify,
            timeout=120,
        )
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
            raise CalDAVServiceError("未找到可用日历，请先检查账号或拉取日历列表。")

        cal = Calendar()
        _ical_add(cal, "prodid", "-//AI Calendar Assistant//Probe Test//EN")
        _ical_add(cal, "version", "2.0")
        event = Event()
        _ical_add(event, "summary", probe_summary)
        _ical_add(event, "uid", probe_uid)
        _ical_add(event, "dtstamp", now)
        _ical_add(event, "dtstart", now)
        _ical_add(event, "dtend", now + timedelta(minutes=30))
        _ical_add(event, "description", "Temporary probe test event for write capability verification.")
        cal.add_component(event)
        ical_str = cal.to_ical().decode()

        saved_obj = None
        saved_href: str | None = None
        try:
            saved_obj = target_cal.save_event(ical_str)
            saved_href = str(getattr(saved_obj, "url", "") or target_cal.url)
            return {
                "ok": True,
                "uid": probe_uid,
                "href": saved_href,
                "summary": probe_summary,
                "calendar_url": str(target_cal.url),
                "message": "日历写入探针成功，临时测试日程已安全清理。",
            }
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"日历写入测试失败：{exc}") from exc
        finally:
            if saved_obj is not None or saved_href is not None:
                deleted = False
                if saved_obj is not None:
                    try:
                        delete_fn = getattr(saved_obj, "delete", None)
                        if callable(delete_fn):
                            delete_fn()
                            deleted = True
                    except Exception as del_exc:
                        logger.warning("通过 saved_obj.delete 删除探针日程失败: %s", del_exc)
                if not deleted:
                    try:
                        self._delete_event_sync(
                            caldav_url, username, password, uid=probe_uid, href=saved_href, ssl_verify=ssl_verify
                        )
                    except Exception as fallback_exc:
                        logger.warning("通过 _delete_event_sync 回退删除探针日程失败: %s", fallback_exc)

    probe_write_permission = probe_write
    _probe_write_permission_sync = _probe_write_sync

    async def create_event(self, caldav_url: str, username: str, password: str, calendar_url: str | None,
                           title: str, start_time: str, end_time: str | None, timezone_str: str | None,
                           location: str | None, description: str | None,
                           reminders: Sequence[ReminderData] | None, recurrence: RecurrenceData,
                           is_all_day: bool, ssl_verify: bool = True, uid: str | None = None) -> CalDAVResult:
        try:
            return await asyncio.to_thread(
                self._create_event_sync, caldav_url, username, password, calendar_url,
                title, start_time, end_time, timezone_str, location, description,
                reminders, recurrence, is_all_day, ssl_verify, uid)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"创建事件失败：{exc}") from exc

    def _create_event_sync(self, caldav_url: str, username: str, password: str, calendar_url: str | None,
                           title: str, start_time: str, end_time: str | None, timezone_str: str | None,
                           location: str | None, description: str | None,
                           reminders: Sequence[ReminderData] | None, recurrence: RecurrenceData,
                           is_all_day: bool, ssl_verify: bool, uid: str | None = None) -> CalDAVResult:
        from dateutil.parser import parse as parse_date
        from icalendar import Alarm, Calendar, Event

        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                    ssl_verify_cert=ssl_verify, timeout=120)
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

        uid = uid or str(uuid.uuid4())
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
                           uid: str | None, href: str | None = None, ssl_verify: bool = True) -> bool:
        try:
            return await asyncio.to_thread(self._delete_event_sync, caldav_url, username, password, uid, href, ssl_verify)
        except Exception:
            return False

    async def update_event(self, caldav_url: str, username: str, password: str,
                           event_data: EventDataPatch, uid: str | None = None,
                           href: str | None = None, ssl_verify: bool = True) -> bool:
        try:
            return await asyncio.to_thread(self._update_event_sync, caldav_url, username, password, event_data, uid, href, ssl_verify)
        except Exception:
            return False

    def _update_event_sync(self, caldav_url: str, username: str, password: str,
                           event_data: EventDataPatch, uid: str | None,
                           href: str | None, ssl_verify: bool) -> bool:
        from icalendar import Calendar as ICal

        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                    ssl_verify_cert=ssl_verify, timeout=120)
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
                        logger.debug("Saving CalDAV event object url=%s uid=%s href=%s", obj.url, uid, href)
                        _ = obj.save()
                        logger.debug("Saved CalDAV event object url=%s uid=%s href=%s", obj.url, uid, href)
                        return True
            except Exception:
                logger.exception("CalDAV update failed while scanning calendar uid=%s href=%s", uid, href)
                continue
        logger.warning("CalDAV update target not found uid=%s href=%s", uid, href)
        return False

    def _delete_event_sync(self, caldav_url: str, username: str, password: str,
                           uid: str | None, href: str | None, ssl_verify: bool) -> bool:
        client = _DAVClient(url=caldav_url.strip(), username=username, password=password,
                                    ssl_verify_cert=ssl_verify, timeout=120)
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


def _get_discovery_urls(url: str) -> list[str]:
    url = url.strip()
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return [url]

    segments = [s for s in path.split("/") if s]
    if not segments:
        return [url]

    is_leaf = False
    if "calendars" in segments:
        idx = segments.index("calendars")
        if idx < len(segments) - 1:
            is_leaf = True
    elif len(segments) >= 2:
        is_leaf = True

    def build_url(p: str) -> str:
        return urlunsplit((parsed.scheme, parsed.netloc, p, parsed.query, parsed.fragment))

    upward_paths: list[str] = []
    cur = list(segments)
    while len(cur) > 0:
        cur.pop()
        if cur:
            upward_paths.append("/" + "/".join(cur) + "/")
        else:
            upward_paths.append("/")

    upward_urls = [build_url(p) for p in upward_paths if build_url(p) != url]
    seen: set[str] = set()
    deduped_upward: list[str] = []
    for u in upward_urls:
        if u not in seen:
            seen.add(u)
            deduped_upward.append(u)

    if is_leaf:
        return deduped_upward + [url]
    return [url] + deduped_upward


def _get_calendar_name(cal: CalendarProtocol, default_name: str = "") -> str:
    name: Any = None
    if hasattr(cal, "get_display_name"):
        try:
            name = cal.get_display_name()
        except Exception:
            pass
    if not name:
        try:
            name = getattr(cal, "name", None)
        except Exception:
            pass
    if not name and default_name:
        name = default_name
    if not name:
        url_str = str(getattr(cal, "url", ""))
        parts = url_str.rstrip("/").split("/")
        name = unquote(parts[-1]) if parts and parts[-1] else url_str
    return str(name)


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
    parts = url.rstrip("/").split("/")
    name = unquote(parts[-1]) if parts and parts[-1] else url
    try:
        cal = client.calendar(url=url, name=name)
    except TypeError:
        cal = client.calendar(url=url)
        try:
            cal.name = name
        except AttributeError:
            pass
    return [cal]


def _ical_add(component: object, name: str, value: object) -> None:
    cast(ICalAddable, component).add(name, value)


def _ical_components(calendar: object) -> Sequence[ICalComponentProtocol]:
    typed_calendar = cast(Any, calendar)
    walk = cast(Callable[[], Sequence[object]], typed_calendar.walk)
    return cast(Sequence[ICalComponentProtocol], walk())


def _parse_caldav_datetime(value: str, timezone_str: str | None) -> datetime:
    from dateutil.parser import parse as parse_date

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
