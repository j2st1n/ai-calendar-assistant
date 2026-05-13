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
            client = caldav.DAVClient(url=url, username=username, password=password)
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
        url = url.strip()
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            raise CalDAVServiceError("未发现任何日历，请检查 CalDAV 账号是否包含日历。")

        results: list[dict[str, str]] = []
        for cal in calendars:
            results.append({
                "name": getattr(cal, "name", "") or str(cal.url),
                "url": str(cal.url),
            })
        return results

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

        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password)
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
        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password)
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
