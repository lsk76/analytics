"""Стан сесії TGStat за знімком сторінки — без браузера, щоб тестувалось окремо.

Маркери взято з живих сторінок tgstat.ru (залогінений Premium-акаунт):
  * `a[data-logout-button]` / `#topbar-userdrop` — лише у залогіненого;
  * `.account-user-name` — ім'я, `.account-position` — тариф («Premium»);
  * `meta[name=csrf-token]` — без нього жоден AJAX-пошук не пройде;
  * заголовок «Just a moment…» / «Attention Required! | Cloudflare» — челендж
    або блок Cloudflare (кліренс прив'язаний до IP, профілю і збірки браузера).
"""
from dataclasses import asdict, dataclass
from typing import Optional

# JS, що знімає з поточної сторінки все потрібне для classify().
SNAPSHOT_JS = """
() => {
    const q = (s) => document.querySelector(s);
    const text = (s) => ((q(s) || {}).textContent || '').trim();
    return {
        url: location.href,
        title: document.title || '',
        csrf: (q('meta[name="csrf-token"]') || {}).content || '',
        logged_in: !!(q('[data-logout-button]') || q('#topbar-userdrop')),
        user: text('.account-user-name'),
        plan: text('.account-position'),
    };
}
"""

OK = "ok"                          # залогінений, Premium, CSRF є
NO_PREMIUM = "no_premium"          # залогінений, але тариф не Premium
LOGIN_REQUIRED = "login_required"  # сторінка відкрилась, користувача немає
CLOUDFLARE = "cloudflare"          # челендж/блок Cloudflare не пройдено
ERROR = "error"                    # браузер не піднявся, таймаут тощо
MANUAL = "manual"                  # іде ручний вхід у звичайному Chrome без Playwright

_CF_TITLES = ("just a moment", "attention required", "cloudflare")


@dataclass
class SessionState:
    state: str
    user: str = ""
    plan: str = ""
    url: str = ""
    title: str = ""
    detail: str = ""
    checked_at: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.state == OK

    def as_dict(self) -> dict:
        return {**asdict(self), "usable": self.usable}


def classify(snap: dict) -> SessionState:
    title = (snap.get("title") or "").strip()
    base = dict(url=snap.get("url") or "", title=title,
                user=snap.get("user") or "", plan=snap.get("plan") or "")
    if any(t in title.lower() for t in _CF_TITLES):
        return SessionState(CLOUDFLARE, detail="Cloudflare не пропускає: "
                            "потрібен вхід через VNC з цього ж браузера", **base)
    if not snap.get("logged_in"):
        return SessionState(LOGIN_REQUIRED, detail="на tgstat немає входу", **base)
    if not snap.get("csrf"):
        return SessionState(ERROR, detail="немає CSRF-токена на сторінці", **base)
    if "premium" not in base["plan"].lower():
        return SessionState(NO_PREMIUM, detail=f"тариф {base['plan']!r}, "
                            "пошук публікацій потребує Premium", **base)
    return SessionState(OK, **base)
