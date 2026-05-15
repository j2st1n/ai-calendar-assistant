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
        client = caldav.DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)
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
        client = caldav.DAVClient(url=url, username=username, password=password, ssl_verify_cert=False, timeout=120)
        errors = []
        for method in [_try_get_calendars, _try_propfind, _try_principal_calendars]:
            try:
                calendars = method(client, url)
                if calendars:
                    return [{"name": getattr(cal, "name", "") or str(cal.url), "url": str(cal.url)} for cal in calendars]
            except Exception as exc:
                errors.append(f"{method.__name__}: {exc}")
        error_detail = "; ".join(errors) if errors else "所有方法均未发现日历"
        raise CalDAVServiceError(f"未发现任何日历。({error_detail})")

    async def create_event(self, caldav_url, username, password, calendar_url, title,
                           start_time, end_time, timezone_str, location, description,
                           reminders, recurrence, is_all_day):
        try:
            return await asyncio.to_thread(
                self._create_event_sync, caldav_url, username, password, calendar_url,
                title, start_time, end_time, timezone_str, location, description,
                reminders, recurrence, is_all_day)
        except CalDAVServiceError:
            raise
        except Exception as exc:
            raise CalDAVServiceError(f"创建事件失败：{exc}") from exc

    def _create_event_sync(self, caldav_url, username, password, calendar_url, title,
                           start_time, end_time, timezone_str, location, description,
                           reminders, recurrence, is_all_day):
        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password,
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
        cal.add("prodid", "-//AI Calendar Assistant//EN")
        cal.add("version", "2.0")
        event = Event()
        event.add("summary", title)
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(timezone.utc))
        from dateutil.parser import parse as parse_date

        if is_all_day:
            dt = parse_date(start_time)
            event.add("dtstart", dt.date())
            if end_time:
                event.add("dtend", parse_date(end_time).date())
        else:
            event.add("dtstart", parse_date(start_time))
            event.add("dtend", parse_date(end_time) if end_time else parse_date(start_time) + timedelta(hours=1))
        if description:
            event.add("description", description)
        if location:
            event.add("location", location)
        rrule = to_rrule(recurrence) if recurrence else None
        if rrule:
            event.add("rrule", rrule)
        cal.add_component(event)
        ical_data = cal.to_ical()
        ical_str = ical_data.decode() if isinstance(ical_data, bytes) else str(ical_data)
        saved = target_cal.save_event(ical_str)
        href = getattr(saved, 'url', str(target_cal.url))
        return {"uid": uid, "href": str(href)}

    async def delete_event(self, caldav_url, username, password, uid, href=None):
        try:
            return await asyncio.to_thread(self._delete_event_sync, caldav_url, username, password, uid, href)
        except Exception:
            return False

    async def update_event(self, caldav_url, username, password, event_data, uid=None, href=None):
        try:
            return await asyncio.to_thread(self._update_event_sync, caldav_url, username, password, event_data, uid, href)
        except Exception:
            return False

    def _update_event_sync(self, caldav_url, username, password, event_data, uid, href):
        from icalendar import Calendar as ICal, Event as ICalEvent
        from datetime import timedelta
        from dateutil.parser import parse as parse_date

        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password,
                                   ssl_verify_cert=False, timeout=120)
        calendars = client.get_calendars()
        for cal in calendars:
            try:
                for obj in cal.objects():
                    obj_url = getattr(obj, 'url', '')
                    obj_uid = getattr(obj, 'id', '')
                    if (href and obj_url == href) or (uid and obj_uid == uid):
                        ical = ICal.from_ical(obj.data)
                        for component in ical.walk():
                            if component.name == 'VEVENT':
                                if event_data.get('title'):
                                    component['summary'] = event_data['title']
                                if event_data.get('start_time'):
                                    new_start = parse_date(event_data['start_time'])
                                    component['dtstart'].dt = new_start
                                    if not event_data.get('end_time') and 'dtend' in component:
                                        component['dtend'].dt = new_start + timedelta(hours=1)
                                if event_data.get('end_time'):
                                    component['dtend'].dt = parse_date(event_data['end_time'])
                                if event_data.get('location'):
                                    component['location'] = event_data['location']
                                if event_data.get('description'):
                                    component['description'] = event_data['description']
                        obj.data = ical.to_ical().decode('utf-8')
                        print(f"[caldav update] saving obj at {getattr(obj, 'url', '?')}", flush=True)
                        obj.save()
                        print(f"[caldav update] save done", flush=True)
                        return True
            except Exception as exc:
                print(f"[caldav update] error: {exc}", flush=True)
                continue
        print(f"[caldav update] target not found uid={uid} href={href}", flush=True)
        return False

    def _delete_event_sync(self, caldav_url, username, password, uid, href):
        client = caldav.DAVClient(url=caldav_url.strip(), username=username, password=password,
                                   ssl_verify_cert=False, timeout=120)
        calendars = client.get_calendars()
        for cal in calendars:
            try:
                for obj in cal.objects():
                    obj_url = getattr(obj, 'url', '')
                    obj_uid = getattr(obj, 'id', '')
                    if (href and obj_url == href) or (uid and obj_uid == uid):
                        obj.delete()
                        print(f"[caldav] deleted uid={uid} href={href}", flush=True)
                        return True
            except Exception as exc:
                print(f"[caldav] delete iter error: {exc}", flush=True)
                continue
        print(f"[caldav] delete not found uid={uid} href={href}", flush=True)
        return False


def _try_get_calendars(client, url):
    return client.get_calendars()


def _try_principal_calendars(client, url):
    return client.principal().calendars()


def _try_propfind(client, url):
    try:
        client.principal()
    except Exception:
        return []
    parts = url.strip("/").split("/")
    cal = client.calendar(url=url)
    cal.name = parts[-1] if parts else url
    return [cal]
