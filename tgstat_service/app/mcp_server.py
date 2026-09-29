"""Окремий MCP-сервер TGStat (stdio), живе в контейнері сервісу tgstat.

Підключення (з ноутбука чи з сервера) — stdio через `docker compose exec -T`:
    ssh tg-analytics 'cd /opt/tg-event-analytics && docker compose \\
        -f docker-compose.yml -f docker-compose.monitor.yml exec -T tgstat \\
        python -m app.mcp_server'
(обгортка — tgstat_service/mcp-stdio.sh, реєстрація — .mcp.json «tgstat»).

Сервер не тримає браузера: кожен інструмент — HTTP до процесу сервісу на
127.0.0.1:8020 (той сам API, що в docs/tgstat-service.md), тож сесія одна, а
темп запитів до tgstat стережеться там само. Відповіді — готовий текст.
"""
from __future__ import annotations

import os
from typing import Annotated, Any, Optional
from urllib.parse import quote

import aiohttp
from pydantic import Field

from mcp.server.mcpserver import MCPServer

API = os.environ.get("TGSTAT_API", "http://127.0.0.1:8020")
# Пошук на кілька сторінок іде хвилинами (пауза між запитами 4–6 с).
TIMEOUT = aiohttp.ClientTimeout(total=float(os.environ.get("TGSTAT_MCP_TIMEOUT", "600")))

mcp = MCPServer(
    name="tgstat",
    instructions=(
        "TGStat (tgstat.ru) через залогінений Premium-акаунт: пошук каналів за "
        "словами, каталоги-підбірки (єдиний шлях до ЧАТІВ — пошуку чатів у tgstat "
        "немає), картка каналу зі статистикою, пошук публікацій, посилання.\n\n"
        "Починай із `tgstat_status`. Кожна сторінка результатів — окремий запит до "
        "tgstat, а він на частоту відповідає капчею «Подозрение на робота» і тоді "
        "відмовляє всім запитам, доки людина не пройде її у VNC. Тому: `max_pages` "
        "тримай малим (1–3), не ганяй той самий пошук по колу, посилання бери з "
        "`tgstat_links` (без запиту до tgstat).\n\n"
        "Капчу й вхід проходить ЛИШЕ людина: `tgstat_manual_login` відкриває "
        "звичайний Chrome у VNC, після входу — закрити вкладку або "
        "`tgstat_manual_finish`. Інструкція — docs/tgstat-service.md."
    ),
)


class ApiError(Exception):
    pass


async def _get(path: str, params: Optional[dict] = None, method: str = "GET") -> Any:
    clean = {k: str(v).lower() if isinstance(v, bool) else str(v)
             for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.request(method, API + path, params=clean) as r:
                body = await r.json(content_type=None)
                if r.status >= 400:
                    raise ApiError(_explain(r.status, body))
                return body
    except aiohttp.ClientConnectorError:
        raise ApiError("сервіс tgstat не відповідає на :8020 — контейнер живий? "
                       "(docker compose ps tgstat / logs tgstat)")


def _explain(status: int, body: dict) -> str:
    state = body.get("state")
    msg = body.get("error") or str(body)[:300]
    if state == "captcha":
        return ("tgstat просить капчу («Подозрение на робота») — запити стоять. "
                "Людина: tgstat_manual_login → у VNC пройти капчу на будь-якій "
                "сторінці даних (напр. tgstat.ru/channel/@rian_ru/stat) → закрити "
                "вкладку. Не повторюй запит до того.")
    if state == "login_required":
        return f"сесія tgstat непридатна: {msg}. Людина: tgstat_manual_login і вхід у VNC."
    if state == "manual":
        return "іде ручний вхід у VNC — дочекайся tgstat_manual_finish."
    return f"{status}: {msg}"


def _n(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:g}"
    return f"{int(value):,}".replace(",", " ")


def _peer_line(i: int, p: dict) -> str:
    head = f"{i}. {p.get('title') or p.get('ref')} — {p.get('ref')}"
    bits = [f"{_n(p.get('subscribers'))} підп."]
    if p.get("avg_post_reach") is not None:
        bits.append(f"охоплення {_n(p['avg_post_reach'])}")
    if p.get("ci") is not None:
        bits.append(f"ІЦ {_n(p['ci'])}")
    if p.get("category"):
        bits.append(p["category"])
    if p.get("last_message"):
        bits.append(f"ост. повідомлення {p['last_message']} тому")
    out = [head, "   " + " · ".join(bits)]
    if p.get("description"):
        out.append("   " + p["description"][:200])
    out.append(f"   {p['tgstat_url']}" + (f" | {p['tme_url']}" if p.get("tme_url") else ""))
    return "\n".join(out)


def _peers(res: dict, title: str) -> str:
    items = res.get("items") or []
    tail = (f"\n\n…є ще (сторінок прочитано {res.get('pages')}); збільш max_pages/limit, "
            "якщо справді треба" if res.get("has_more") else "")
    if not items:
        return f"{title}: нічого не знайдено (сторінок {res.get('pages')})"
    body = "\n".join(_peer_line(i, p) for i, p in enumerate(items, 1))
    return f"{title}: {res['count']}\n\n{body}{tail}"


def _safe(fn):
    """Помилки — текстом «⚠ …», як у решті MCP-інструментів репо."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ApiError as e:
            return f"⚠ {e}"
    return wrapper


@mcp.tool(description="Стан сесії tgstat: залогінений акаунт, тариф, чи немає "
                      "капчі/Cloudflare. reload=true — перезайти на сайт (1 запит).")
@_safe
async def tgstat_status(reload: bool = False) -> str:
    h = await _get("/health")
    s = await _get("/auth/status", {"reload": "1" if reload else ""})
    line = f"стан: {s['state']}" + (f" · {s['user']} ({s['plan']})" if s.get("user") else "")
    out = [line, f"браузер: {'є' if h.get('browser') else 'немає'}"
           f"{' · іде ручний вхід' if h.get('manual') else ''}",
           f"перевірено: {s.get('checked_at')}"]
    if s.get("detail"):
        out.append(f"деталі: {s['detail']}")
    if not s.get("usable"):
        out.append("що робити: " + _explain(503, {"state": s["state"],
                                                  "error": s.get("detail")}))
    return "\n".join(out)


@mcp.tool(description="Пошук КАНАЛІВ tgstat за словами в назві (in_about=true — і в "
                      "описі). Кожна сторінка (~30 каналів) — окремий запит до tgstat.")
@_safe
async def tgstat_channels_search(
    q: Annotated[str, Field(description="слова для пошуку, напр. «Бурятия»")],
    in_about: Annotated[bool, Field(description="шукати й в описі каналу")] = False,
    min_subs: Annotated[Optional[int], Field(description="мінімум підписників")] = None,
    max_subs: Annotated[Optional[int], Field(description="максимум підписників")] = None,
    country: Annotated[str, Field(description="країна (назва рос. або id); '' — будь-яка")] = "Россия",
    category: Annotated[str, Field(description="категорія tgstat рос., напр. «Политика», «Новости и СМИ»")] = "",
    language: Annotated[str, Field(description="мова рос., напр. «Русский», «Башкирский»")] = "",
    sort: Annotated[str, Field(description="participants | avg_reach | ci_index | members_7d | members_30d")] = "participants",
    limit: int = 50,
    max_pages: Annotated[int, Field(description="скільки сторінок (запитів) читати, 1–20")] = 2,
) -> str:
    res = await _get("/channels/search", dict(
        q=q, in_about=in_about, min_subs=min_subs, max_subs=max_subs,
        country=country, category=category, language=language, sort=sort,
        limit=limit, max_pages=max_pages))
    return _peers(res, f"Канали за «{q}»")


@mcp.tool(description="Список підбірок tgstat: kind=geo — регіональні (напр. "
                      "buratia-region), theme — тематичні. Slug іде в tgstat_catalog.")
@_safe
async def tgstat_catalog_tags(kind: str = "geo") -> str:
    res = await _get("/catalog/tags", {"kind": kind})
    if not res["items"]:
        return f"підбірок {kind} не знайдено (розмітка змінилась? /raw?path=/tags/{kind})"
    return f"Підбірки {kind}: {res['count']}\n\n" + "\n".join(
        f"{t['slug']} — {t['title']}" for t in res["items"])


@mcp.tool(description="Канали або ЧАТИ підбірки tgstat (регіональної/тематичної). "
                      "Єдиний спосіб знайти чати — пошуку чатів у tgstat немає.")
@_safe
async def tgstat_catalog(
    tag: Annotated[str, Field(description="slug підбірки з tgstat_catalog_tags, напр. buratia-region")],
    kind: Annotated[str, Field(description="channel або chat")] = "chat",
    limit: int = 100,
    max_pages: int = 3,
) -> str:
    res = await _get(f"/catalog/{quote(tag)}", {"kind": kind, "limit": limit,
                                                 "max_pages": max_pages})
    what = "Чати" if kind == "chat" else "Канали"
    return _peers(res, f"{what} підбірки {tag} ({res['tgstat_url']})")


_STAT_LABELS = {
    "subscribers": "підписники", "ci": "індекс цитування",
    "avg_post_reach": "середнє охоплення 1 поста", "avg_ad_reach": "рекламне охоплення",
    "err_percent": "ERR", "er_percent": "ER", "age": "вік", "posts": "публікацій",
}


@mcp.tool(description="Картка каналу/чату з tgstat: підписники і приріст, індекс "
                      "цитування, охоплення, ERR/ER, вік, кількість публікацій, "
                      "категорія, гео, РКН + посилання. 1 запит до tgstat.")
@_safe
async def tgstat_channel(
    ref: Annotated[str, Field(description="@handle, handle, t.me/handle або URL tgstat")],
    kind: Annotated[str, Field(description="channel або chat; порожнє — з посилання або channel")] = "",
) -> str:
    c = await _get(f"/channel/{quote(ref, safe='@')}", {"kind": kind})
    out = [f"{c.get('title')} — {c.get('ref')}" + (" ✔" if c.get("verified") else ""),
           f"категорія: {c.get('category') or '—'} · гео/мова: {c.get('geo_lang') or '—'}"
           f"{' · зареєстрований у РКН' if c.get('rkn_registered') else ''}"]
    if c.get("description"):
        out.append(f"опис: {c['description'][:400]}")
    for key, st in (c.get("stats") or {}).items():
        val = st.get("value")
        line = f"{_STAT_LABELS.get(key, key)}: {val if key == 'age' else _n(val)}"
        det = st.get("details") or {}
        if det:
            line += " (" + ", ".join(f"{k} {v if isinstance(v, str) else _n(v)}"
                                     for k, v in det.items()) + ")"
        out.append(line)
    out.append(f"{c.get('tgstat_stat_url')}" + (f" | {c['tme_url']}" if c.get("tme_url") else ""))
    return "\n".join(out)


@mcp.tool(description="Пошук ПУБЛІКАЦІЙ у tgstat (Premium) — у каналах і чатах. "
                      "Перша сторінка ~20 постів, кожна наступна — ще запит.")
@_safe
async def tgstat_posts_search(
    q: Annotated[str, Field(description="запит, як у пошуку tgstat")],
    date_from: Annotated[str, Field(description="YYYY-MM-DD")] = "",
    date_to: Annotated[str, Field(description="YYYY-MM-DD")] = "",
    peer_type: Annotated[str, Field(description="all | channel | chat")] = "all",
    sort: Annotated[str, Field(description="date | views")] = "date",
    hide_forwards: bool = False,
    strong: Annotated[bool, Field(description="точний збіг форми слова")] = False,
    extended: Annotated[bool, Field(description="розширений синтаксис tgstat")] = False,
    minus_words: Annotated[str, Field(description="слова-виключення")] = "",
    limit: int = 40,
    max_pages: int = 2,
) -> str:
    res = await _get("/posts/search", {
        "q": q, "from": date_from, "to": date_to, "peer_type": peer_type,
        "sort": sort, "hide_forwards": hide_forwards, "strong": strong,
        "extended": extended, "minus_words": minus_words, "limit": limit,
        "max_pages": max_pages})
    items = res.get("items") or []
    head = f"Публікації за «{q}»: знайдено {_n(res.get('total'))}, показано {len(items)}"
    if not items:
        return head
    rows = []
    for i, p in enumerate(items, 1):
        rows.append(
            f"{i}. {p.get('date')} · {p.get('channel_title')} ({p.get('ref')}) · "
            f"👁 {_n(p.get('views'))}\n   {(p.get('text') or '')[:300].replace(chr(10), ' ')}\n"
            f"   {p.get('tme_post_url') or ''} | {p.get('tgstat_post_url')}")
    tail = ("\n\n…є ще сторінки; збільш max_pages, якщо справді треба"
            if res.get("has_more") else "")
    return head + "\n\n" + "\n".join(rows) + tail


@mcp.tool(description="Посилання на tgstat (сторінка, статистика, пост) і t.me для "
                      "каналу/чату — БЕЗ запиту до tgstat.")
@_safe
async def tgstat_links(ref: str, post_id: Optional[int] = None, kind: str = "") -> str:
    res = await _get(f"/links/{quote(ref, safe='@')}", {"post_id": post_id, "kind": kind})
    return "\n".join(f"{k}: {v}" for k, v in res.items() if v)


@mcp.tool(description="ЗМІНЮЄ СТАН. Ручний вхід/капча: сервіс відпускає свій браузер і "
                      "запускає звичайний Chrome (без автоматизації) у VNC. Далі ЛЮДИНА "
                      "проходить вхід/капчу; інші запити до tgstat до завершення стоять.")
@_safe
async def tgstat_manual_login() -> str:
    await _get("/auth/manual", method="POST")
    return ("Звичайний Chrome відкрито у VNC. Людині: ssh -N -L 6080:127.0.0.1:6080 "
            "tg-analytics → http://localhost:6080/vnc.html → увійти / пройти капчу → "
            "закрити вкладку (або tgstat_manual_finish).")


@mcp.tool(description="ЗМІНЮЄ СТАН. Завершити ручний вхід: закрити звичайний Chrome "
                      "(cookies зберігаються) і повернути браузер сервісу. Повертає стан.")
@_safe
async def tgstat_manual_finish() -> str:
    s = await _get("/auth/manual/finish", method="POST")
    return f"стан: {s['state']}" + (f" · {s['user']} ({s['plan']})" if s.get("user") else "") + \
        (f"\n{s['detail']}" if s.get("detail") else "")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
