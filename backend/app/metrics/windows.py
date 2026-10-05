"""Half-open reporting windows of complete local days."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class WindowError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    start: date  # inclusive local date
    end: date  # exclusive local date

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def utc_bounds(self, tz: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(tz)
        start = datetime.combine(self.start, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(self.end, time.min, tzinfo=zone).astimezone(UTC)
        return start, end

    def weekday_profile(self) -> Counter[int]:
        return Counter((self.start + timedelta(days=i)).weekday() for i in range(self.days))

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Window:
        return cls(date.fromisoformat(d["start"]), date.fromisoformat(d["end"]))


def default_windows(as_of: date, days: int = 7) -> tuple[Window, Window]:
    """Last ``days`` complete local days before ``as_of`` versus the preceding ``days`` days.

    ``as_of`` itself is treated as incomplete and excluded.
    """
    current = Window(as_of - timedelta(days=days), as_of)
    baseline = Window(as_of - timedelta(days=2 * days), as_of - timedelta(days=days))
    return baseline, current


def validate_pair(baseline: Window, current: Window, as_of: date) -> None:
    for name, w in (("baseline", baseline), ("current", current)):
        if w.days <= 0:
            raise WindowError(f"{name} window must have start < end")
        if w.days > 92:
            raise WindowError(f"{name} window is longer than 92 days")
        if w.end > as_of:
            raise WindowError(f"{name} window ends after the as-of date {as_of.isoformat()} (incomplete day)")
    if baseline.days != current.days:
        raise WindowError("baseline and current windows must have equal duration")
    if baseline.weekday_profile() != current.weekday_profile():
        raise WindowError("baseline and current windows must cover the same weekdays")
    if baseline.end > current.start:
        raise WindowError("baseline window must end on or before the current window starts")


def local_midnight_utc(d: date, tz: str) -> datetime:
    return datetime.combine(d, time.min, tzinfo=ZoneInfo(tz)).astimezone(UTC)
