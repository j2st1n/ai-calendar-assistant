def _try_quick_modify(text: str, existing: dict) -> dict | None:
    import re
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    old_st = existing.get("start_time", "")
    if not old_st or "T" not in old_st:
        return None
    date_part = old_st.split("T")[0]
    new_st = f"{date_part}T{h:02d}:{mi:02d}:00+08:00"
    existing["start_time"] = new_st
    return existing