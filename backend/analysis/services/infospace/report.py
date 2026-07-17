"""Дайджест-звіт по обраних подіях: одне речення на новину у стилі щоденного
інформзведення.

Стиль (з прикладів замовника, файл `25.06.26 Інфо`): плоский СПИСОК без
заголовків республік і без посилань; кожен пункт — ОДНЕ фактологічне речення
українською, що починається з МАЛОЇ літери й закінчується КРАПКОЮ З КОМОЮ («;»).

Генерація .docx — за підходом `distribution/export` (адмін-дія → готовий файл на
завантаження), але без Node/docxtemplater (їх немає в образі) — мінімалістично
через `python-docx`.
"""
import asyncio
import io
import json
import logging

from django.conf import settings

from analysis.services import llm

logger = logging.getLogger(__name__)

# Речень за один виклик LLM (щоб не впертись у ліміт токенів на велику вибірку)
BATCH = 30

DIGEST_PROMPT = """Ти — редактор щоденного інформаційного зведення про 8 національних республік РФ.
Отримуєш список новин (id + короткий опис українською). Для КОЖНОЇ напиши стислий
опис про те, про що новина — ІДЕАЛЬНО ОДНЕ речення, щонайбільше ДВА.

ЖОРСТКІ ПРАВИЛА ФОРМАТУ (стиль зведення):
- 1 речення (ідеально), максимум 2; українською, фактологічно, без оцінок і без вступних слів;
- починається з МАЛОЇ літери;
- закінчується КРАПКОЮ З КОМОЮ «;» (НЕ крапкою);
- без посилань, без нумерації, без назви республіки як окремого заголовка;
- стисло й по суті, як у прикладах нижче.

ПРИКЛАДИ СТИЛЮ:
- мер улан-уде і. шутенков перевірив готовність спортивно-оздоровчого табору «старт» до нового сезону;
- глава республіки саха а. ніколаєв посів четверте місце у «національному рейтингу»;
- у казані стартував фінальний етап регіонального відбору військово-патріотичних зборів «гвардієць»;
- парламентська делегація татарстану візьме участь у XIII форумі регіонів білорусі та росії;

Відповідь — СТРОГО JSON без розмітки, збережи всі id зі входу:
{"items":[{"id":<id>,"sentence":"<речення з малої літери, ; в кінці>"}, ...]}
"""


def _round_quotes(s: str) -> str:
    """Будь-які прямі/інші лапки → криві «ялинки»-парою “ ” (“ на відкритті, ” далі)."""
    out, open_q = [], True
    for ch in s:
        if ch in '"“”„«»':
            out.append("“" if open_q else "”")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)


def _clean_sentence(s: str) -> str:
    """Гарантуємо три правила: мала перша літера, кінцева «;», криві лапки “ ”."""
    s = " ".join((s or "").split()).strip()
    if not s:
        return ""
    s = s[0].lower() + s[1:]
    s = s.rstrip(" .;…!?")
    return _round_quotes(s + ";") if s else ""


async def _agen(events, model, system, api_key=None):
    payload = [{"id": e.id, "summary": (e.summary or "")[:800]} for e in events]
    user = "НОВИНИ:\n" + json.dumps(payload, ensure_ascii=False)
    raw = await llm.query(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        model=model, json_mode=True, max_tokens=4000, api_key=api_key)
    data = llm.extract_json(raw) or {}
    out = {}
    for it in (data.get("items") or []):
        try:
            out[int(it["id"])] = _clean_sentence(it.get("sentence"))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def generate_digest_sentences(events, model=None, api_key=None):
    """{event_id: sentence} у стилі зведення. Порожній рядок — якщо LLM не дав.
    Промпт — з таблиці Setting['digest_report_prompt'] (порожньо = DIGEST_PROMPT).
    api_key — ключ юзера, що генерує звіт (порожньо = глобальний)."""
    from analysis.models import Setting
    events = list(events)
    model = model or settings.LLM_MODEL
    system = Setting.get("digest_report_prompt", DIGEST_PROMPT)
    out = {}
    for i in range(0, len(events), BATCH):
        try:
            out.update(asyncio.run(_agen(events[i:i + BATCH], model, system, api_key)))
        except Exception as e:  # noqa: BLE001 — не валимо весь звіт через один батч
            logger.warning("digest batch %d: %r", i, e)
    return out


def build_digest_docx(rows) -> bytes:
    """rows: список рядків-речень. Плоский список абзаців → .docx (bytes)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.font.italic = True
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for line in rows:
        if line:
            p = doc.add_paragraph(line)
            p.paragraph_format.first_line_indent = Cm(1.25)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
