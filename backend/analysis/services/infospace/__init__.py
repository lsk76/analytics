"""
Конвеєр «Моніторинг інформаційного простору» (pipeline="infospace").

Безперервний полінг джерел (Telegram-акаунти / RSS / сайти) → Post →
AI-скрін релевантності → зіставлення з живими подіями (ковзне вікно) → Event.

Структура пакета:
  adapters/   — спільний інтерфейс джерела (RawItem, BaseSourceAdapter, ADAPTERS)
  scrapers.py — реєстр кастомних web-скраперів (SCRAPERS)
  utils.py    — canonical_url та інші спільні утиліти
  prompts.py  — дефолтні промпти скріну/судді (робочі копії — в полях задачі)
  stages.py   — stage-ранери воркерів (Phase 1)

Дизайн: docs/infospace-monitoring-pipeline.md
"""
