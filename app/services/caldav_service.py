import asyncio
import logging

import caldav
from caldav.lib.error import AuthorizationError, DAVError

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
        url = url.strip().rstrip("/")
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
        url = url.strip().rstrip("/")
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
