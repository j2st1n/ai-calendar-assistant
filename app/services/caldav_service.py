import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import caldav
from caldav.lib.error import AuthorizationError, DAVError
from icalendar import Calendar, Event

from app.calendar.recurrence import to_rrule

logger = logging.getLogger(__name__)


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
        try:
            client = caldav.DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)
            principal = client.principal()
            if not principal:
                raise CalDAVServiceError("无法获取 CalDAV principal，请检查 URL。")
        except AuthorizationError:
            raise CalDAVServiceError("认证失败，请检查用户名和密码。")
        except DAVError as exc:
            raise CalDAVServiceError(f"连接失败：{exc.reason if getattr(exc, 'reason', None) else exc}")
        except CalDAVServiceError:
            raise

    async def list_calendars(self, url: str, username: str, password: str) -> list[dict[str, str]]:
        try:
            return await asyncio.to_thread(self._list_calendars_sync, url, username, password)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"拉取日历列表失败：{exc}") from exc

    def _list_calendars_sync(self, url: str, username: str, password: str) -> list[dict[str, str]]:
        url = url.strip().rstrip("/")
        client = caldav.DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)

        errors = []
        for method in [_try_propfind, _try_get_calendars, _try_principal_calendars]:
            try:
                calendars = method(client, url)
                if calendars:
                    results: list[dict[str, str]] = []
                    for cal in calendars:
                        results.append({
                            "name": getattr(cal, "name", "") or str(cal.url),
                            "url": str(cal.url),
                        })
                    return results
            except Exception as exc:
                errors.append(f"{method.__name__}: {exc}")

        error_detail = "; ".join(errors) if errors else "所有方法均未发现日历"
        raise CalDAVServiceError(f"未发现任何日历，请检查 CalDAV 账号是否包含日历。({error_detail})")


def _try_get_calendars(client, url: str) -> list:
    return client.get_calendars()


def _try_principal_calendars(client, url: str) -> list:
    return client.principal().calendars()


def _try_propfind(client, url: str) -> list:
    principal = client.principal()
    if principal is None:
        return []

    home_url = str(principal.url).strip()
    try:
        home_url = client._make_absolute_url(home_url)
    except Exception:
        pass

    try:
        resp = client.propfind(home_url, props=["{DAV:}resourcetype", "{DAV:}displayname"], depth=1)
        if resp.status // 100 == 2:
            resp.find_objects_and_props()
            objects = getattr(resp, 'objects', None) or {}
            if objects:
                calendars = _parse_calendar_objects(client, objects, home_url)
                if calendars:
                    return calendars
    except Exception:
        pass

    try:
        cal = client.calendar(home_url)
        cal.get_display_name()
        return [cal]
    except Exception:
        pass

    return []


def _parse_calendar_objects(client, objects, home_url) -> list:
    calendars = []
    for href, props in objects.items():
        if not href:
            continue
        if href == home_url or href == home_url.rstrip("/") or href.rstrip("/") == home_url.rstrip("/"):
            continue
        try:
            cal = client.calendar(href)
            for prop_element in (props or {}).values() if isinstance(props, dict) else []:
                if prop_element is None:
                    continue
                text = getattr(prop_element, 'text', None)
                if text:
                    cal.name = text
                    break
            if not getattr(cal, 'name', None):
                parts = href.strip("/").split("/")
                cal.name = parts[-1] if parts else href
            calendars.append(cal)
        except Exception:
            continue
    return calendars

    async def create_event(
        self,
        caldav_url: str,
        username: str,
        password: str,
        calendar_url: str,
        title: str,
        start_time: str,
        end_time: str | None,
        timezone_str: str,
        location: str | None,
        description: str | None,
        reminders: list[dict] | None,
        recurrence: dict | None,
        is_all_day: bool,
    ) -> dict | None:
        try:
            return await asyncio.to_thread(
                self._create_event_sync,
                caldav_url, username, password, calendar_url,
                title, start_time, end_time, timezone_str,
                location, description, reminders, recurrence, is_all_day,
            )
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"创建事件失败：{exc}") from exc

    def _create_event_sync(self, *args) -> dict | None:
        (
            caldav_url, username, password, calendar_url,
            title, start_time, end_time, timezone_str,
            location, description, reminders, recurrence, is_all_day,
        ) = args

        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password, ssl_verify_cert=False, timeout=120)
        principal = client.principal()
        calendars = principal.calendars()
        target_cal = None
        for cal in calendars:
            if str(cal.url) == calendar_url.strip():
                target_cal = cal
                break
        if target_cal is None and calendars:
            target_cal = calendars[0]
        if target_cal is None:
            raise CalDAVServiceError("找不到目标日历。")

        uid = str(uuid.uuid4())
        cal = Calendar()
        cal.add("prodid", "-//AI Calendar Assistant//EN")
        cal.add("version", "2.0")

        event = Event()
        event.add("summary", title)
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(timezone.utc))

        if is_all_day:
            from dateutil.parser import parse as parse_date
            dt = parse_date(start_time)
            event.add("dtstart", dt.date())
            if end_time:
                event.add("dtend", parse_date(end_time).date())
        else:
            from dateutil.parser import parse as parse_date
            dt_start = parse_date(start_time)
            event.add("dtstart", dt_start)
            if end_time:
                event.add("dtend", parse_date(end_time))
            else:
                event.add("dtend", dt_start + timedelta(hours=1))

        if description:
            event.add("description", description)
        if location:
            event.add("location", location)

        rrule = to_rrule(recurrence) if recurrence else None
        if rrule:
            event.add("rrule", rrule)

        cal.add_component(event)
        target_cal.save_event(cal.to_ical().decode() if isinstance(cal.to_ical(), bytes) else cal.to_ical())
        return {"uid": uid, "href": str(target_cal.url)}

    async def delete_event(
        self, caldav_url: str, username: str, password: str, uid: str,
    ) -> bool:
        try:
            return await asyncio.to_thread(self._delete_event_sync, caldav_url, username, password, uid)
        except Exception:
            return False

    def _delete_event_sync(self, caldav_url: str, username: str, password: str, uid: str) -> bool:
        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password, ssl_verify_cert=False, timeout=120)
        principal = client.principal()
        for cal in principal.calendars():
            try:
                events = cal.search(event_uid=uid)
                if events:
                    events[0].delete()
                    return True
            except Exception:
                 continue
        return False
