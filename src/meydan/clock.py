"""Time for an instance: one zone, one place that converts.

Storage is UTC (aware datetimes / `timestamptz`). Conversion to local time happens only
through a `Clock`. Agents get pre-rendered text from it: models don't reliably convert time
zones or compute weekdays (or even the year), so they should never have to.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Locale:
    months: tuple[str, ...]  # January first
    weekdays: tuple[str, ...]  # Monday first


TR = Locale(
    months=(
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    ),
    weekdays=("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"),
)  # fmt: skip
EN = Locale(
    months=(
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    weekdays=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
)  # fmt: skip


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Clock:
    """An instance's clock. `source` is injectable so tests and simulations can freeze time."""

    zone: str
    locale: Locale = EN
    source: Callable[[], datetime] = field(default=_utcnow, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)

    def now(self) -> datetime:
        return self.source().astimezone(self.tz)

    def today(self) -> date:
        return self.now().date()

    def to_local(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            raise ValueError("Naive datetime: store and pass timezone-aware datetimes only.")
        return dt.astimezone(self.tz)

    def format_date(self, d: date | datetime) -> str:
        """Day, month name, weekday: '31 Ekim Cumartesi' / '31 October Saturday'."""
        if isinstance(d, datetime):
            d = self.to_local(d).date()
        return f"{d.day} {self.locale.months[d.month - 1]} {self.locale.weekdays[d.weekday()]}"

    def format_datetime(self, dt: datetime) -> str:
        """Local date and time: '31 Ekim Cumartesi 18:00'."""
        local = self.to_local(dt)
        return f"{self.format_date(local)} {local:%H:%M}"

    def format_window(self, start: date, end: date) -> str:
        """A half-open day range [start, end) at the precision it has, e.g. for a tournament
        whose exact day isn't fixed yet: a whole month -> 'Aralık 2026', one day ->
        '13 Aralık Cumartesi', anything else -> '13 Aralık Cumartesi – 14 Aralık Pazar'."""
        last = end - timedelta(days=1)
        if (
            start.day == 1
            and end.day == 1
            and (end.year * 12 + end.month) - (start.year * 12 + start.month) == 1
        ):
            return f"{self.locale.months[start.month - 1]} {start.year}"
        if last == start:
            return self.format_date(start)
        return f"{self.format_date(start)} – {self.format_date(last)}"

    def calendar(self, days: int = 14) -> list[str]:
        """The next `days` dates from today: ['2026-10-26 26 Ekim Pazartesi', ...].

        For agent context: the ISO date is for tool arguments, the text for replies.
        """
        start = self.today()
        dates = (start + timedelta(days=i) for i in range(days))
        return [f"{d.isoformat()} {self.format_date(d)}" for d in dates]
