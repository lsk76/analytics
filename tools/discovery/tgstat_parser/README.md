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
