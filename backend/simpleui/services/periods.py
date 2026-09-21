"""Єдиний контрол секції — період. Пресети + «свій»."""
from dataclasses import dataclass
from datetime import date, timedelta

PRESETS = (
    ("week", "Тиждень", 7),
    ("month", "Місяць", 30),
    ("quarter", "Квартал", 91),
)
PRESET_DAYS = {k: d for k, _, d in PRESETS}


@dataclass
class Period:
    key: str          # week|month|quarter|custom
    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    @property
    def gran(self) -> str:
        """Гранулярність графіка динаміки: до ~45 днів — по днях, далі по тижнях."""
        return "day" if self.days <= 45 else "week"

    @property
    def label(self) -> str:
        for k, lbl, _ in PRESETS:
            if k == self.key:
                return lbl.lower()
        return f"{self.date_from:%d.%m.%Y} — {self.date_to:%d.%m.%Y}"

    def query(self) -> str:
        if self.key in PRESET_DAYS:
            return f"period={self.key}"
        return f"period=custom&from={self.date_from.isoformat()}&to={self.date_to.isoformat()}"


def _parse(s):
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def period_from_request(request, today=None) -> Period:
    today = today or date.today()
    key = request.GET.get("period", "week")
    if key == "custom":
        d_from, d_to = _parse(request.GET.get("from")), _parse(request.GET.get("to"))
        if d_from and d_to and d_from <= d_to:
            return Period("custom", d_from, d_to)
        key = "week"
    if key not in PRESET_DAYS:
        key = "week"
    return Period(key, today - timedelta(days=PRESET_DAYS[key] - 1), today)
