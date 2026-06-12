"""
Парсить збережені HTML сторінки з tgstat_pages/ і оновлює Google Sheets.

Збирає: Назва, Підписники, Категорія, Мова/Гео, Опис (TGStat)

Запуск:
    python3 04_parse_pages.py --test    # тільки перші 10 файлів
    python3 04_parse_pages.py           # всі файли
"""
import os, re, sys
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

PAGES_DIR   = '/Users/r2d2/Documents/dev/openclaw/tgstat_pages'
KEY_FILE    = '/Users/r2d2/Downloads/tg-vk-groups-baa6d186e7f5.json'
SHEET_ID    = '1qnhnIa_1ziZjRICiP4RHBi0OJlF28uyf_3LDPnIMbOA'

TEST_MODE = '--test' in sys.argv

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds  = Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
gc     = gspread.authorize(creds)
ws     = gc.open_by_key(SHEET_ID).worksheet('Аркуш1')

# Читаємо таблицю — знаходимо рядок за посиланням (колонка D)
print("Читаємо таблицю...", flush=True)
links_col = ws.col_values(4)   # D: https://t.me/{handle}
handle_to_row = {}
for i, link in enumerate(links_col[1:], start=2):
    h = link.replace('https://t.me/', '').strip()
    if h:
        handle_to_row[h.lower()] = i

print(f"Рядків у таблиці: {len(handle_to_row)}", flush=True)

def parse_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = {}

    # Назва з title тегу: "Ариг Ус | Бурятия — @arigus — TGStat"
    title_tag = soup.find('title')
    if title_tag:
        raw = title_tag.get_text(strip=True)
        raw = re.sub(r'\s*—\s*@\w+\s*—\s*TGStat.*', '', raw)
        raw = re.sub(r'Telegram-канал\s+["“„]?', '', raw)
        raw = raw.strip(' ""“„')
        data['name'] = raw

    # Підписники: <h2 class="mb-1 text-dark">112 672</h2>
    h2 = soup.select_one('h2.mb-1.text-dark')
    if h2:
        digits = re.sub(r'\D', '', h2.get_text(strip=True))
        data['subscribers'] = int(digits) if digits else ''

    # Категорія
    for b in soup.find_all('b'):
        if 'Категори' in b.get_text():
            nxt = b.next_sibling
            cat = str(nxt).strip() if nxt else ''
            if not cat:
                cat = b.parent.get_text(strip=True).replace('Категория:', '').strip()
            data['category'] = cat
            break

    # Мова / Гео
    for b in soup.find_all('b'):
        if 'язык' in b.get_text().lower() or 'гео' in b.get_text().lower():
            raw = b.parent.get_text(separator=' ', strip=True)
            raw = re.sub(r'Гео и язык канала:', '', raw).strip()
            data['geo_lang'] = raw
            break

    # Опис (og:description)
    og = soup.find('meta', property='og:description')
    if og and og.get('content'):
        data['description'] = re.sub(r'\s+', ' ', og['content']).strip()

    return data

# Обробляємо файли
files = sorted(f for f in os.listdir(PAGES_DIR) if f.endswith('.html'))
if TEST_MODE:
    files = files[:10]
    print(f"⚠ TEST MODE: {len(files)} файлів", flush=True)
else:
    print(f"Файлів: {len(files)}", flush=True)

updates = []
not_found = []

for fname in files:
    handle = fname.replace('.html', '').lower()
    row = handle_to_row.get(handle)
    if not row:
        not_found.append(handle)
        continue

    with open(os.path.join(PAGES_DIR, fname), encoding='utf-8') as f:
        html = f.read()

    d = parse_page(html)
    if not d:
        continue

    # Формуємо оновлення для конкретних клітинок цього рядка
    # A=Назва, G=Підписники, K=Категорія, L=Мова, N=Опис(TGStat)
    row_updates = []
    if d.get('name'):
        row_updates.append({'range': f'A{row}', 'values': [[d['name']]]})
    if d.get('subscribers'):
        row_updates.append({'range': f'G{row}', 'values': [[d['subscribers']]]})
    if d.get('category'):
        row_updates.append({'range': f'K{row}', 'values': [[d['category']]]})
    if d.get('geo_lang'):
        row_updates.append({'range': f'L{row}', 'values': [[d['geo_lang']]]})
    if d.get('description'):
        row_updates.append({'range': f'N{row}', 'values': [[d['description']]]})

    updates.extend(row_updates)
    print(f"  @{handle}: {d.get('name','')} | {d.get('subscribers','')} | {d.get('category','')} | {d.get('geo_lang','')}", flush=True)

print(f"\nОновлень: {len(updates)}, не знайдено в таблиці: {len(not_found)}", flush=True)
if not_found:
    print(f"Не знайдено: {not_found[:5]}", flush=True)

# Записуємо батчами по 500
for i in range(0, len(updates), 500):
    ws.batch_update(updates[i:i+500])
    print(f"✓ Записано {i+1}–{min(i+500, len(updates))}", flush=True)

print("\n✓ ГОТОВО!")
