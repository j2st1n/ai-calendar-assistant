from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Intent(str, Enum):
    create_event = "create_event"
    update_event = "update_event"
    delete_event = "delete_event"
    provide_missing_fields = "provide_missing_fields"
    no_event = "no_event"


class Reminder(BaseModel):
    minutes_before: int = 30


class Recurrence(BaseModel):
    frequency: Literal["daily", "weekly", "monthly"]
    interval: int = 1
    days_of_week: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] = Field(default_factory=list)
    day_of_month: int | None = None
    until: str | None = None
    count: int | None = None


class CalendarEvent(BaseModel):
    title: str
    start_time: str
    end_time: str | None = None
    timezone: str = "Asia/Shanghai"
    location: str | None = None
    description: str | None = None
    reminders: list[Reminder] = Field(default_factory=lambda: [Reminder()])
    recurrence: Recurrence | None = None
    is_all_day: bool = False


class ExtractionResult(BaseModel):
    intent: Intent
    event: CalendarEvent | None = None
    events: list[CalendarEvent] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    unsupported_reason: str | None = None
    confidence: float = 0.0
