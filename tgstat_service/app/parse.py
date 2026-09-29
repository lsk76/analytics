"""Парсери відповідей tgstat.ru — чисті функції (без браузера), під тести.

Розмітку знято з живого tgstat.ru (залогінений Premium, 2026-09-29) і з
../sm-analytics (пошук публікацій). Фікстури — tests/fixtures/.

Що звідки:
  * пошук каналів  POST /channels/search -> JSON {status, html, hasMore, nextPage,
    nextOffset}; html — картки `.peer-item-row`;
  * каталоги       POST /tag/<тег>/items (peerType=channel|chat) -> той самий
    JSON; картки `.peer-item-box`. Окремого пошуку чатів у tgstat НЕМАЄ
    (`/chats/search` дає 500) — чати лише через каталоги;
  * картка каналу  GET /channel/@h/stat — шапка + картки «значення + підписи»;
  * публікації     POST /search (HTML сторінки) і далі POST /search/list (JSON)
    — картки `.post-container`.
"""
import html as html_lib
import json
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

# https://tgstat.ru/channel/@h, https://tgstat.com/ru/chat/@h/stat,
# https://tgstat.ru/chat/uVK0KI5dQewzYzNi (приватний — без @)
PEER_URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)?tgstat\.(?:ru|com)(?:/[a-z]{2})?/(channel|chat)/(@?[\w-]+)")
POST_PATH_RE = re.compile(r"/(channel|chat)/(@?[\w-]+)/(\d+)")
TTTTT_RE = re.compile(r"https?://ttttt\.me/([\w-]+)/(\d+)")
CAPTCHA_MARKERS = ("Подозрение на робота", "recaptcha-widget")


class Restricted(Exception):
    """tgstat запідозрив робота (429 + reCAPTCHA) — капчу проходить людина у VNC."""


def check_restricted(text: str) -> None:
    head = text[:5000]
    if '"status":"restricted"' in head.replace(" ", "") or any(
            m in head for m in CAPTCHA_MARKERS):
        raise Restricted("tgstat просить капчу («Подозрение на робота»): "
                         "пройди її у VNC, запити до того не підуть")


def parse_json(text: str) -> dict:
    check_restricted(text)
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"tgstat віддав не JSON: {text[:200]!r}")
    if data.get("status") != "ok":
        raise ValueError(f"tgstat status={data.get('status')!r}")
    return data


def number(raw: Optional[str]) -> Optional[float]:
    """'108 566' -> 108566, '3.3k' -> 3300, '1.2M' -> 1200000, '7%' -> 7.0,
    '-4 107' -> -4107, '—' -> None."""
    if not raw:
        return None
    s = raw.replace("\xa0", " ").replace(" ", "").replace(",", ".").strip()
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)([kKmMкК]?)%?", s)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"k": 1e3, "к": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)
    val *= mult
    return int(val) if val.is_integer() and "%" not in s else val


def _text(el: Optional[Tag]) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def links(kind: str, ref: str, base: str = "https://tgstat.ru",
          post_id: Optional[int] = None) -> dict:
    """Посилання на tgstat і Telegram для каналу/чату (і поста, якщо є)."""
    base = base.rstrip("/")
    peer = f"{base}/{kind}/{ref}"
    out = {"tgstat_url": peer, "tgstat_stat_url": f"{peer}/stat"}
    handle = ref[1:] if ref.startswith("@") else None
    out["tme_url"] = f"https://t.me/{handle}" if handle else None
    if post_id:
        out["tgstat_post_url"] = f"{peer}/{post_id}"
        out["tme_post_url"] = f"https://t.me/{handle}/{post_id}" if handle else None
    return out


def _peer(kind: str, ref: str, base: str) -> dict:
    return {"kind": kind, "handle": ref[1:] if ref.startswith("@") else None,
            "ref": ref, **links(kind, ref, base)}


# --- пошук каналів і каталоги ------------------------------------------------

_COUNTER_KEYS = (
    ("подписчик", "subscribers"), ("участник", "subscribers"),
    ("охват", "avg_post_reach"), ("цитирован", "ci"),
)


def parse_peers(html: str, base: str = "https://tgstat.ru") -> list[dict]:
    """Картки каналів/чатів: і рядки пошуку (.peer-item-row), і плитки
    каталогу (.peer-item-box)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out, seen = [], set()
    for card in soup.select(".peer-item-row, .peer-item-box"):
        a = next((a for a in card.select("a[href]") if PEER_URL_RE.search(a["href"])), None)
        if not a:
            continue
        kind, ref = PEER_URL_RE.search(a["href"]).groups()
        if ref in seen:
            continue
        seen.add(ref)
        item = _peer(kind, ref, base)
        item["title"] = _text(card.select_one(".font-16.text-dark"))
        desc = card.select_one(".line-clamp-2")
        if desc:
            item["description"] = _text(desc)
        # Рядок пошуку: пари «h4 значення / підпис». Плитка: «<b>N</b> участников».
        for h4 in card.select("h4"):
            label = _text(h4.find_next_sibling("div")).lower()
            for needle, key in _COUNTER_KEYS:
                if needle in label and key not in item:
                    item[key] = number(_text(h4))
        for b in card.select("b"):
            label = _text(b.parent).lower()
            for needle, key in _COUNTER_KEYS[:2]:
                if needle in label and key not in item:
                    item[key] = number(_text(b))
        cat = card.select_one(".border.rounded.bg-light") or card.select_one(
            ".font-12.text-body")
        if cat:
            item["category"] = _text(cat)
        last = card.select_one("[data-original-title*='Последнее сообщение']")
        if last:
            item["last_message"] = _text(last)
        out.append(item)
    return out


# --- картка каналу -----------------------------------------------------------

_CARD_KEYS = (
    ("подписчики", "subscribers"), ("участники", "subscribers"),
    ("индекс цитирования", "ci"),
    ("средний рекламный", "avg_ad_reach"), ("средний охват", "avg_post_reach"),
    ("возраст", "age"), ("вовлеченность подписчиков (err)", "err_percent"),
    ("вовлеченность подписчиков (er)", "er_percent"),
    ("подписчиков читают", "readers_percent"),
)
_DETAIL_KEYS = {
    "сегодня": "today", "вчера": "yesterday", "за неделю": "week",
    "за месяц": "month", "уп. каналов": "mentioning_channels",
    "упоминаний": "mentions", "репостов": "reposts", "err": "err",
    "err24": "err24", "за 12 часов": "12h", "за 24 часа": "24h",
    "за 48 часов": "48h", "канал создан": "created", "чат создан": "created",
    "добавлен в tgstat": "added_to_tgstat", "пересылки": "forwards",
    "комментарии": "comments", "реакции": "reactions",
}


def _detail_key(label: str) -> str:
    low = label.lower()
    if low.startswith("err"):          # «ERR<sub>24</sub>» -> «err 24» -> err24
        low = low.replace(" ", "")
    return _DETAIL_KEYS.get(low, label)


def _card_key(label: str) -> Optional[str]:
    for needle, key in _CARD_KEYS:
        if needle in label:
            return key
    return None


def parse_channel_stat(html: str, base: str = "https://tgstat.ru") -> dict:
    """GET /channel/@h/stat -> шапка + зведені показники."""
    check_restricted(html)
    soup = BeautifulSoup(html or "", "html.parser")
    out: dict = {}
    title = soup.select_one("h1")
    out["title"] = _text(title)
    out["verified"] = bool(title and title.select_one(".tg-verified-icon"))
    own = soup.select_one('link[rel=canonical]') or soup.select_one(
        'a.btn-info[href*="/channel/"], a.btn-info[href*="/chat/"]')
    m = PEER_URL_RE.search(own["href"]) if own and own.get("href") else None
    if m:
        out.update(_peer(*m.groups(), base))
    for b in soup.select("b"):
        label = _text(b).rstrip(":").lower()
        if label.startswith("гео и язык"):
            out["geo_lang"] = _text(b.parent).split(":", 1)[-1].strip()
        elif label == "категория" and "category" not in out:
            out["category"] = _text(b.parent).split(":", 1)[-1].strip()
    desc = soup.select_one("p.card-text")
    out["description"] = _text(desc)
    out["rkn_registered"] = "Зарегистрирован в РКН" in soup.get_text()

    stats: dict = {}
    for card in soup.select(".card.card-body"):
        h2 = card.select_one("h2")
        label_el = card.select_one(".position-absolute.text-uppercase")
        if not h2 or not label_el:
            continue
        label = _text(label_el).lower()
        key = _card_key(label)
        if not key:
            continue
        value_raw = _text(h2)
        entry = {"value": number(value_raw) if key != "age" else value_raw,
                 "raw": value_raw}
        details = {}
        for row in card.select("tr"):
            cells = row.select("td")
            if len(cells) >= 2:
                details[_detail_key(_text(cells[1]))] = number(_text(cells[0]))
        for b in card.select("b.font-20"):
            span = b.find_next_sibling("span")
            details[_detail_key(_text(span))] = _text(b)
        if details:
            entry["details"] = details
        stats.setdefault(key, entry)
    # «344 941 всего» — картка публікацій без підпису-заголовка в кутку.
    for card in soup.select(".card.card-body"):
        h2 = card.select_one("h2")
        if h2 and "всего" in _text(h2):
            total = number(_text(h2).replace("всего", ""))
            det = {_detail_key(_text(r.select("td")[1])): number(_text(r.select("td")[0]))
                   for r in card.select("tr") if len(r.select("td")) >= 2}
            stats["posts"] = {"value": total, "details": det}
    out["stats"] = stats
    return out


# --- пошук публікацій --------------------------------------------------------

_FOUND_RE = re.compile(r"Найдено:\s*<b>([\d\s ]+)</b>", re.I)


def found_count(html: str) -> Optional[int]:
    m = _FOUND_RE.search(html or "")
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def nothing_found(html: str) -> bool:
    return "Ничего не найдено" in (html or "")


def search_form_state(html: str) -> dict:
    """Поля #search-form зі сторінки результатів: page/offset там — уже
    наступна сторінка, їх і шлемо в /search/list."""
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.select_one("#search-form")
    if not form:
        return {}
    state = {}
    for inp in form.select("input[name=page], input[name=offset]"):
        state[inp["name"]] = inp.get("value", "")
    return state


def _post_text(el: Tag) -> str:
    """Текст посту: <br> -> перенос, підсвітку збігів (<mark>) склеюємо без
    розривів — інакше «<mark>al</mark>ımda» стає двома рядками."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    raw = html_lib.unescape(el.get_text(""))
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n"))
    return "\n".join(line for line in lines if line)


def parse_posts(html: str, base: str = "https://tgstat.ru") -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    out, seen = [], set()
    for card in soup.select(".post-container"):
        kind = ref = post_id = None
        for a in card.select("a[href], a[data-src]"):
            m = POST_PATH_RE.search(a.get("href") or a.get("data-src") or "")
            if m:
                kind, ref, post_id = m.group(1), m.group(2), int(m.group(3))
                break
        tme = card.select_one('a[href*="ttttt.me"]')
        if not ref and tme:
            m = TTTTT_RE.search(tme["href"])
            if m:
                kind, ref, post_id = "channel", "@" + m.group(1), int(m.group(2))
        if not ref:
            continue
        key = (ref, post_id)
        if key in seen:
            continue
        seen.add(key)
        item = _peer(kind, ref, base)
        item.update(links(kind, ref, base, post_id))
        item["post_id"] = post_id
        head = card.select_one(".post-header h5 a")
        item["channel_title"] = _text(head)
        item["date"] = _text(card.select_one(".post-header p.text-muted small"))
        item["text"] = "\n".join(filter(None, map(_post_text, card.select(".post-text"))))
        for a in card.select("[data-original-title]"):
            tip = a["data-original-title"].lower()
            val = number(_text(a))
            if "просмотров" in tip:
                item["views"] = val
            elif "поделились" in tip:
                item["shares"] = val
            elif "пересылок" in tip:
                item["forwards"] = val
        out.append(item)
    return out


def parse_tags(html: str) -> list[dict]:
    """/tags/geo, /tags/theme -> [{slug, title}] (без дублів)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="/tag/"]'):
        m = re.search(r"/tag/([\w-]+)/?$", a["href"])
        if not m or m.group(1) in seen:
            continue
        title = _text(a)
        if not title:
            continue
        seen.add(m.group(1))
        out.append({"slug": m.group(1), "title": title})
    return out
