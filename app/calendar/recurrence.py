from typing import Any, Sequence

from app.ai.schemas import Recurrence


def to_rrule(recurrence: Recurrence | dict[str, Any] | None) -> str | None:
    if recurrence is None:
        return None
    freq = ""
    interval = 1
    days_of_week: Sequence[str] = []
    day_of_month: int | None = None
    until: str | None = None
    count: int | None = None

    if isinstance(recurrence, Recurrence):
        freq = recurrence.frequency.upper()
        interval = recurrence.interval
        days_of_week = recurrence.days_of_week
        day_of_month = recurrence.day_of_month
        until = recurrence.until
        count = recurrence.count
    else:
        freq = str(recurrence.get("frequency", "")).upper()
        interval = int(recurrence.get("interval", 1))
        days_of_week = recurrence.get("days_of_week") or []
        day_of_month = recurrence.get("day_of_month")
        until = recurrence.get("until")
        count = recurrence.get("count")

    parts = [f"FREQ={freq}", f"INTERVAL={interval}"]
    if days_of_week:
        parts.append(f"BYDAY={','.join(days_of_week)}")
    if day_of_month:
        parts.append(f"BYMONTHDAY={day_of_month}")
    if until:
        from dateutil.parser import parse as parse_date
        dt = parse_date(until)
        parts.append(f"UNTIL={dt.strftime('%Y%m%dT000000Z')}")
    if count:
        parts.append(f"COUNT={count}")
    return ";".join(parts)
