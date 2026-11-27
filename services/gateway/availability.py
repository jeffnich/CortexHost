"""Free/busy from a calendar feed, reduced to bare time blocks.

The gateway reads an iCalendar (ICS) feed, ideally one the calendar provider
already publishes as free/busy only (Google: share the calendar publicly with
"See only free/busy (hide details)"). Either way, this module keeps nothing
but start and end times: titles, attendees, locations and descriptions are
never read into the response, so even a full-detail feed can't leak through it.
"""

import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

MAX_FEED_BYTES = 2_000_000
MAX_DAYS = 14
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _http_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cortex-gateway/1"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - URL is owner config
        data = resp.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("calendar feed too large")
    return data


def parse_work_days(spec: str) -> set[int]:
    """'mon-fri' or 'mon,wed,fri' -> weekday numbers (Mon=0)."""
    days: set[int] = set()
    for part in (spec or "mon-fri").lower().replace(" ", "").split(","):
        if "-" in part:
            a, b = (_DAY_NAMES.index(x[:3]) for x in part.split("-", 1))
            days.update(range(a, b + 1))
        elif part:
            days.add(_DAY_NAMES.index(part[:3]))
    return days


class FreeBusy:
    def __init__(
        self,
        url: str,
        tz: str = "UTC",
        work_hours: str = "09:00-17:00",
        work_days: str = "mon-fri",
        cache_s: int = 600,
        fetch=_http_fetch,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.url = url
        self.tz = ZoneInfo(tz or "UTC")
        start, end = (work_hours or "09:00-17:00").split("-")
        self.day_start = datetime.strptime(start.strip(), "%H:%M").time()
        self.day_end = datetime.strptime(end.strip(), "%H:%M").time()
        self.work_days = parse_work_days(work_days)
        self.cache_s = cache_s
        self._fetch = fetch
        self._now = now
        self._cal = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _calendar(self):
        with self._lock:
            if self._cal is None or time.monotonic() - self._fetched_at > self.cache_s:
                self._cal = icalendar.Calendar.from_ical(self._fetch(self.url))
                self._fetched_at = time.monotonic()
            return self._cal

    def _busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        out = []
        for ev in recurring_ical_events.of(self._calendar()).between(start, end):
            if str(ev.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
                continue
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            s = ev.get("DTSTART").dt
            e = ev.get("DTEND").dt if ev.get("DTEND") else None
            if not isinstance(s, datetime):  # all-day: busy for whole local days, DTEND exclusive
                e = e or s + timedelta(days=1)
                s = datetime.combine(s, datetime.min.time(), self.tz)
                e = datetime.combine(e, datetime.min.time(), self.tz)
            elif e is None:
                e = s
            s = s if s.tzinfo else s.replace(tzinfo=self.tz)
            e = e if e.tzinfo else e.replace(tzinfo=self.tz)
            out.append((max(s, start), min(e, end)))
        out.sort()
        merged: list[tuple[datetime, datetime]] = []
        for s, e in out:
            if e <= s:
                continue
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def availability(self, start_date: str | None = None, days: int = 5) -> dict:
        """Working-hours free and busy blocks per day, in the owner's time zone."""
        days = max(1, min(int(days or 5), MAX_DAYS))
        today = self._now().astimezone(self.tz).date()
        first = date.fromisoformat(start_date) if start_date else today
        if first < today:
            first = today
        if first > today + timedelta(days=60):
            raise ValueError("start_date must be within the next 60 days")
        window_start = datetime.combine(first, datetime.min.time(), self.tz)
        window_end = window_start + timedelta(days=days)
        busy = self._busy_intervals(window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc))

        now_local = self._now().astimezone(self.tz)
        result = []
        for i in range(days):
            d = first + timedelta(days=i)
            if d.weekday() not in self.work_days:
                continue
            ds = max(datetime.combine(d, self.day_start, self.tz), now_local)
            de = datetime.combine(d, self.day_end, self.tz)
            if de <= ds:
                continue
            day_busy = [(max(s.astimezone(self.tz), ds), min(e.astimezone(self.tz), de))
                        for s, e in busy if s < de and e > ds]
            free, cursor = [], ds
            for s, e in day_busy:
                if s > cursor:
                    free.append((cursor, s))
                cursor = max(cursor, e)
            if cursor < de:
                free.append((cursor, de))
            fmt = lambda blocks: [{"start": s.isoformat(timespec="minutes"), "end": e.isoformat(timespec="minutes")}
                                  for s, e in blocks if (e - s) >= timedelta(minutes=15)]
            result.append({"date": d.isoformat(), "free": fmt(free), "busy": fmt(day_busy)})
        return {
            "timezone": str(self.tz),
            "working_hours": f"{self.day_start:%H:%M}-{self.day_end:%H:%M}",
            "days": result,
        }
