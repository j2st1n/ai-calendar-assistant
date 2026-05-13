class CalDavClient:
    async def test_connection(self) -> bool:
        return False

    async def list_calendars(self) -> list[dict[str, str]]:
        return []
