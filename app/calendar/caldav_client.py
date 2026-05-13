from app.services.caldav_service import CalDAVService


class CalDavClient:
    def __init__(self) -> None:
        self._service = CalDAVService()
        self._url: str | None = None
        self._username: str | None = None
        self._password: str | None = None

    async def test_connection(self) -> bool:
        if not self._url or not self._username or not self._password:
            return False
        try:
            await self._service.test_connection(self._url, self._username, self._password)
            return True
        except Exception:
            return False

    async def list_calendars(self) -> list[dict[str, str]]:
        if not self._url or not self._username:
            return []
        try:
            return await self._service.list_calendars(self._url, self._username, self._password or "")
        except Exception:
            return []
