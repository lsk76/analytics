# TGStat Parser

Збирає канали з TGStat по регіонах РФ і завантажує в Google Sheets.

## Файли

| Файл | Опис |
|------|------|
| `01_collect_handles.py` | Збирає хендли каналів з TGStat (з пагінацією, фільтр >800 підписників) |
| `02_enrich_and_upload.py` | Збагачує дані через TGStat + Telegram і записує в Google Sheets |

## Залежності

```
pip install playwright gspread google-auth google-api-python-client beautifulsoup4 requests
playwright install chrome
```

## Запуск

### Крок 1 — зібрати хендли

```bash
python3 -u 01_collect_handles.py > collect.log 2>&1 &
# Відкриється Chrome. Залогінтесь на tgstat.ru
touch /tmp/go.flag
# Результат: /tmp/tgstat_results.json
```

### Крок 2 — збагатити і завантажити

```bash
python3 -u 02_enrich_and_upload.py > enrich.log 2>&1 &
# Відкриється Chrome (збережена сесія)
touch /tmp/go.flag
# Записує в Google Sheets кожні 50 каналів
```

## Важливо

- **Профіль браузера** зберігається в `/tmp/tgstat_profile` — повторний логін не потрібен
- **Сесія TGStat** прив'язана до IP і TLS-fingerprint Chrome, тому запити йдуть через реальний браузер
- Ключ Google Sheets: `/Users/r2d2/Downloads/tg-vk-groups-baa6d186e7f5.json`
- Таблиця: `1qnhnIa_1ziZjRICiP4RHBi0OJlF28uyf_3LDPnIMbOA`

## Регіони

Бурятія, Саха, Тива, Татарстан, Башкортостан, Чечня, Інгушетія, Дагестан

---

## Задача «прикордонні чати» (11 суб'єктів, 47 точок)

Окрема пара скриптів — оригінальні `01`/`02` не чіпаються, вони під регіони + Sheets.

| Файл | Опис |
|------|------|
| `border_01_collect.py` | Збір хендлів по НАСЕЛЕНИХ ПУНКТАХ із сітки `backend/analysis/fixtures/border_points.json` (47 точок, 126 запитів: канали + чати) |
| `border_02_enrich.py` | Збагачення (TGStat + прев'ю t.me) → CSV `backend/_dir/border_tgstat_channels.csv` |

Відмінності від `01`/`02`:

- запит — місто/райцентр, а не назва республіки;
- поріг підписників **на точку** (`min_subs` 100–800): для села на 3–14 тис.
  дефолтні 800 відсікають геть усе;
- `inAbout=1` для малих точок — «Подслушано» часто без назви міста в заголовку;
- шукаються ще й **чати** (`/chats/search`); якщо TGStat такого не віддає, скрипт
  фіксує це і йде далі лише по каналах;
- профіль браузера **не в `/tmp`** (дефолт `~/.tgstat_profile`) — інакше його стирає
  кожне перезавантаження;
- обидва ідемпотентні: прогін можна зупиняти й продовжувати;
- вихід — CSV, не Google Sheets.

```bash
python3 -u border_01_collect.py > collect.log 2>&1 &
# відкриється Chrome — залогінитись на tgstat.ru, потім:
touch ~/.tgstat_go.flag
python3 -u border_02_enrich.py > enrich.log 2>&1 &
```

Env: `TGSTAT_PROFILE`, `TGSTAT_GO_FLAG`, `TGSTAT_OUT`, `TGSTAT_CSV`,
`TGSTAT_POINTS` (кома-перелік точок для часткового прогону), `TGSTAT_LIMIT`,
`TGSTAT_MAX_PAGES`.

План задачі: `docs/border-regions-chats-plan.md`.
