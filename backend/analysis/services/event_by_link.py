"""Подія за посиланням: користувач вставляє лише URL, решту заповнює програма.

Шлях: fetch(url) → текст поста/статті → скрін-промпт дослідження (той самий,
що в infospace: relevant/summary/region/tags) → Post(stage=done) →
stages._create_event (гео через resolve_region, теги через tags.resolve) →
Event approved. Нового конвеєра нема — переюзано існуючі стадії.

Telegram читаємо через публічний embed-віджет t.me (без акаунта й gateway):
підходить для відкритих каналів; закритий канал/чат → зрозуміла помилка.
"""
from __future__ import annotations

import asyncio
import html as _html
import re
from dataclasses import dataclass
from datetime import datetime, timezone as _tz
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from django.utils import timezone as djtz

from analysis.models import AnalysisTask, Channel, Event, Post, Source
from analysis.services import llm

_TME = re.compile(r"^https?://(?:www\.)?t\.me/(?:s/)?(?P<name>[A-Za-z0-9_]{4,})/(?P<mid>\d+)")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


class LinkError(Exception):
    """Помилка, яку показуємо користувачу як є (людською мовою)."""


@dataclass
class Fetched:
    url: str
    title: str
    text: str
    posted_at: datetime | None
    channel: Channel | None = None
    source: Source | None = None
    channel_name: str = ""


# --------------------------------------------------------------------------- fetch

def _strip_html(fragment: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    s = re.sub(r"</(p|div)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s).strip()


def _fetch_telegram(url: str, m: re.Match) -> Fetched:
    name, mid = m.group("name"), m.group("mid")
    canonical = f"https://t.me/{name}/{mid}"
    try:
        r = httpx.get(f"{canonical}?embed=1", headers={"User-Agent": _UA},
                      timeout=20, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:  # noqa: BLE001
        raise LinkError(f"Не вдалося відкрити t.me: {e}") from e
    page = r.text
    mt = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S)
    if not mt:
        raise LinkError("Telegram не віддає текст цього поста: канал закритий, пост "
                        "видалений або це не пост каналу. Вставте посилання на "
                        "відкритий канал.")
    text = _strip_html(mt.group(1))
    md = re.search(r'datetime="([^"]+)"', page)
    posted = None
    if md:
        try:
            posted = datetime.fromisoformat(md.group(1).replace("Z", "+00:00"))
        except ValueError:
            posted = None
    mo = re.search(r'tgme_widget_message_owner_name[^>]*>\s*<span[^>]*>(.*?)</span>', page, re.S)
    owner = _strip_html(mo.group(1)) if mo else name
    from analysis.services.directory import telegram_url
    url_key = telegram_url(name)
    channel = (Channel.objects.filter(url=url_key).first()
               or Channel.objects.filter(username__iexact=name).order_by("-fetched_at", "-id").first())
    if channel is None:
        channel = Channel.objects.create(username=name, title=owner[:512], url=url_key,
                                         platform="telegram", chat_type="channel")
    elif not channel.url:
        channel.url = url_key
        channel.save(update_fields=["url"])
    return Fetched(url=canonical, title="", text=text, posted_at=posted,
                   channel=channel, channel_name=(channel.title or name)[:128])


def _fetch_web(url: str) -> Fetched:
    from analysis.services.infospace.adapters.web import WebAdapter, _get
    try:
        page = _get(url)
    except httpx.HTTPError as e:  # noqa: BLE001
        raise LinkError(f"Не вдалося відкрити сторінку: {e}") from e
    d = WebAdapter._extract_trafilatura(page)
    if not (d.get("text") or "").strip():
        raise LinkError("На сторінці не знайшлося тексту статті. Перевірте посилання "
                        "або вставте посилання на саму новину, а не на розділ.")
    host = urlparse(url).netloc.lower().removeprefix("www.")
    source = next((s for s in Source.objects.filter(kind__in=(Source.KIND_WEB, Source.KIND_RSS))
                   if host and host in (s.url or "").lower()), None)
    posted = d.get("date")
    if posted is not None and posted.tzinfo is None:
        posted = posted.replace(tzinfo=_tz.utc)
    return Fetched(url=url, title=(d.get("title") or "")[:500], text=d["text"],
                   posted_at=posted, source=source,
                   channel_name=(source.name if source else host)[:128])


def fetch(url: str) -> Fetched:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise LinkError("Вставте повне посилання, що починається з http:// або https://")
    m = _TME.match(url)
    return _fetch_telegram(url, m) if m else _fetch_web(url)


# --------------------------------------------------------------------------- screen

# Скрін відсіює «не тему» (спорт, погода…). Але подію додає ЛЮДИНА — релевантність
# уже вирішена, тож у фолбеку просимо лише заповнити картку, без суду про тему.
# Ті самі поля й мова, що у скріні; блок тегів — зі схеми дослідження.
FILL_PROMPT = """\
You fill an EVENT CARD for a media-monitoring database of the Russian
Federation regions. A human editor has ALREADY decided this item belongs to
the database — do NOT judge relevance, do NOT refuse.

You receive ONE item (title + text). Respond with STRICT JSON only, no
markdown fences, no commentary:

{"relevant": true,
 "signature": "one line: WHO did WHAT WHERE — canonical fact fingerprint",
 "summary": "2-3 sentences for the event card, factual, no speculation",
 "region": "<RF subject name if clearly identifiable from text, else null>"}

Rules:
- "summary" and "signature" MUST be written in UKRAINIAN (translate the facts
  from the source language into Ukrainian). Never answer in Russian.
- "region" — the official Ukrainian name of the RF subject (e.g. "Кемеровська
  область", "Саха (Якутія)").
"""


def _fill_prompt(task: AnalysisTask, screen_system: str) -> str:
    """FILL_PROMPT + блок тегів, який стадія скріну додає зі схеми дослідження."""
    marker = "Додай у JSON поле"
    tag_block = screen_system[screen_system.find(marker):] if marker in screen_system else ""
    return FILL_PROMPT + ("\n" + tag_block if tag_block else "")


def screen(task: AnalysisTask, fetched: Fetched) -> dict:
    """Скрін-промпт дослідження (relevant/summary/region/tags) на одному тексті."""
    from analysis.services.infospace.stages import _build_screen_prompt, _llm_screen
    from django.conf import settings
    model = task.info_screen_model or task.llm_model or settings.LLM_MODEL
    fake = SimpleNamespace(id=0, title=fetched.title, text=fetched.text)
    system = _build_screen_prompt(task)
    key = llm.key_for_user(task.owner)
    verdict, _ = asyncio.run(_llm_screen([fake], system, model, key)).get(0, (None, True))
    if isinstance(verdict, dict) and not (verdict.get("summary") or "").strip():
        # скрін відсіяв як «не тему» → заповнюємо картку без суду про тему
        verdict, _ = asyncio.run(_llm_screen([fake], _fill_prompt(task, system), model, key)
                                 ).get(0, (None, True))
    if not isinstance(verdict, dict):
        raise LinkError("ШІ не відповів (перевантаження або невалідна відповідь). "
                        "Спробуйте ще раз за хвилину.")
    return verdict


# --------------------------------------------------------------------------- create

def create_event(task: AnalysisTask, url: str, user=None) -> tuple[Event, bool]:
    """→ (подія, створено?). Пост із таким URL у дослідженні вже є і має подію →
    повертаємо її (created=False), нічого не дублюючи."""
    from analysis.services.stages import _create_event
    existing = Post.objects.filter(task=task, url=url).select_related("event").first()
    if existing and existing.event_id:
        return existing.event, False

    f = fetch(url)
    if existing is None and f.url != url:
        existing = Post.objects.filter(task=task, url=f.url).select_related("event").first()
        if existing and existing.event_id:
            return existing.event, False
    v = screen(task, f)
    posted = f.posted_at or djtz.now()
    cls = {
        "signature": (v.get("signature") or "").strip(),
        "summary": (v.get("summary") or "").strip(),
        "screen_reason": (v.get("reason") or "").strip(),
        "region": (v.get("region") or "").strip() if v.get("region") else "",
        "tags": v.get("tags") or {},
        "_screen_model": task.info_screen_model or task.llm_model or "",
        "_added_by_link": True,
    }
    if existing is None:
        from analysis.services.infospace.stages import _content_hash
        post = Post.objects.create(
            task=task, url=f.url, stage=Post.STAGE_DONE, is_relevant=True,
            channel=f.channel, source=f.source, channel_name=f.channel_name,
            title=f.title, text=f.text, posted_at=posted,
            region_subject=(f.source.region_subject if f.source else
                            (f.channel.region_subject if f.channel else None)),
            content_hash=_content_hash(f.text), classification=cls)
    else:
        post = existing
        post.classification = {**(post.classification or {}), **cls}
        post.is_relevant = True
        post.save(update_fields=["classification", "is_relevant"])
    ev = _create_event(task, [post])
    who = getattr(user, "username", "") or "user"
    ev.review_status = Event.REVIEW_APPROVED       # додано людиною — довіряємо
    ev.review_notes = f"manual: додано за посиланням ({who})"
    ev.reviewed_at = djtz.now()
    ev.last_post_at = posted
    ev.save(update_fields=["review_status", "review_notes", "reviewed_at", "last_post_at"])
    return ev, True
