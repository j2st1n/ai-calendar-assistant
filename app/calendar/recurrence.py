from app.ai.schemas import Recurrence


def to_rrule(recurrence: Recurrence | None) -> str | None:
    if recurrence is None:
        return None
    parts = [f"FREQ={recurrence.frequency.upper()}", f"INTERVAL={recurrence.interval}"]
    if recurrence.days_of_week:
        parts.append(f"BYDAY={','.join(recurrence.days_of_week)}")
    if recurrence.day_of_month:
        parts.append(f"BYMONTHDAY={recurrence.day_of_month}")
    if recurrence.until:
        parts.append(f"UNTIL={recurrence.until}")
    if recurrence.count:
        parts.append(f"COUNT={recurrence.count}")
    return ";".join(parts)
