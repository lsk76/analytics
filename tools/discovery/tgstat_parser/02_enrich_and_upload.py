import asyncio, json, os, re, sys, time, html as htmllib
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import gspread, requests
from google.oauth2.service_account import Credentials

# python3 02_enrich_and_upload.py --test  → тільки перші 10 каналів
TEST_MODE = '--test' in sys.argv

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds  = Credentials.from_service_account_file(
    '/Users/r2d2/Downloads/tg-vk-groups-baa6d186e7f5.json', scopes=SCOPES)
gc = gspread.authorize(creds)
ws = gc.open_by_key('1qnhnIa_1ziZjRICiP4RHBi0OJlF28uyf_3LDPnIMbOA').worksheet('Аркуш1')

HEADERS = [
    'Назва', 'Платформа', 'Регіон', 'Посилання', 'TGStat',
    'Тип', 'Підписники', 'Серед. охоплення', 'ERR%',
    'Постів/тижд.', 'Категорія', 'Мова', 'Опис (Telegram)', 'Опис (TGStat)'
]

with open('/tmp/tgstat_results.json') as f:
    raw = json.load(f)

channels = []
for region, items in raw.items():
    seen = set()
    for item in items:
        h = item['handle']
        if h not in seen:
            seen.add(h)
            channels.append({'region': region, 'handle': h})

if TEST_MODE:
    channels = channels[:10]
    print(f"⚠ TEST MODE: обробляємо тільки {len(channels)} каналів", flush=True)
else:
    print(f"Всього каналів: {len(channels)}", flush=True)

def parse_subs(raw):
    """'11 287 subscribers' → 11287"""
    digits = re.sub(r'[^\d]', '', raw)
    return int(digits) if digits else ''

def tg_info(handle):
    try:
        r = requests.get(f'https://t.me/{handle}',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        t = r.text
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', t)
        desc_m  = re.search(r'<meta property="og:description" content="([^"]+)"', t)
        subs_m  = re.search(r'<div class="tgme_page_extra">([^<]+)</div>', t)
        title = htmllib.unescape(title_m.group(1)) if title_m else ''
        desc  = re.sub(r'\s+', ' ', htmllib.unescape(desc_m.group(1))).strip() if desc_m else ''
        subs  = subs_m.group(1).strip() if subs_m else ''
        if 'Telegram: Contact' in title:
            title = ''
        return title, desc, subs
    except:
        return '', '', ''

def parse_tgstat_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = {}

    # Назва — беремо з og:title або <title>, уникаємо коротких елементів
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        data['name'] = htmllib.unescape(og['content'])
    else:
        title_tag = soup.find('title')
        raw_title = title_tag.get_text(strip=True) if title_tag else ''
        # TGStat format: "Channel Name - TGStat"
        data['name'] = raw_title.split(' - TGStat')[0].split(' — TGStat')[0].strip()

    # Опис
    og_desc = soup.find('meta', property='og:description')
    if og_desc and og_desc.get('content'):
        data['description'] = re.sub(r'\s+', ' ', htmllib.unescape(og_desc['content'])).strip()
    else:
        desc_el = soup.select_one('.channel-description, [class*="description"], .about')
        data['description'] = desc_el.get_text(strip=True) if desc_el else ''

    # Лічильники
    counters = {}
    for block in soup.select('[class*="count"], [class*="stat"], [class*="counter"], .card-body'):
        label_el = block.select_one('[class*="label"], [class*="title"], small, span')
        value_el = block.select_one('[class*="value"], [class*="number"], b, strong')
        if label_el and value_el:
            label = label_el.get_text(strip=True).lower()
            value = value_el.get_text(strip=True)
            counters[label] = value

    for key in ['subscribers', 'подписчик', 'учасник', 'members']:
        for k, v in counters.items():
            if key in k:
                data['subscribers'] = v
                break

    for key in ['reach', 'охват', 'coverage']:
        for k, v in counters.items():
            if key in k:
                data['avg_reach'] = v
                break

    for k, v in counters.items():
        if 'err' in k or 'вовлечен' in k or 'engagement' in k:
            data['err'] = v
            break

    for k, v in counters.items():
        if 'публикац' in k or 'пост' in k or 'post' in k:
            data['posts_per_week'] = v
            break

    for tag in soup.select('.badge, [class*="category"], [class*="tag"], [class*="lang"]'):
        txt = tag.get_text(strip=True)
        if txt and len(txt) < 40:
            if not data.get('category'):
                data['category'] = txt
            elif not data.get('language'):
                data['language'] = txt

    return data

# Буфер для батч-запису (кожні 10 рядків — один API-запит)
WRITE_EVERY = 10

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir='/tmp/tgstat_profile',
            channel='chrome',
            headless=False,
            args=['--disable-blink-features=AutomationControlled'],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto('https://tgstat.ru', wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)
        print("TGStat відкрито", flush=True)

        ws.clear()
        ws.update(values=[HEADERS], range_name='A1')
        ws.format('A1:N1', {
            'textFormat': {'bold': True},
            'backgroundColor': {'red': 0.2, 'green': 0.4, 'blue': 0.7},
        })

        buffer = []   # [(sheet_row, row_data)]
        errors = 0

        def flush_buffer():
            if not buffer:
                return
            # Групуємо послідовні рядки в один update-запит
            buffer.sort(key=lambda x: x[0])
            start = buffer[0][0]
            rows_data = [r for _, r in buffer]
            ws.update(values=rows_data, range_name=f'A{start}')
            buffer.clear()

        for i, ch in enumerate(channels):
            handle = ch['handle']
            region = ch['region']
            sheet_row = i + 2  # рядок в таблиці (1=заголовок)

            tg_name, tg_desc, tg_subs = tg_info(handle)

            tgs_data = {}
            try:
                await page.goto(
                    f'https://tgstat.ru/channel/@{handle}',
                    wait_until='domcontentloaded',
                    timeout=15000
                )
                await page.wait_for_timeout(1000)
                tgs_data = parse_tgstat_page(await page.content())
            except Exception as e:
                errors += 1

            tgs_name = tgs_data.get('name', '')
            name = tg_name or (tgs_name if len(tgs_name) > 2 else '') or handle
            subs = parse_subs(tgs_data.get('subscribers') or tg_subs or '')

            row = [
                name, 'TG', region,
                f'https://t.me/{handle}',
                f'https://tgstat.ru/channel/@{handle}',
                '',
                subs,
                tgs_data.get('avg_reach', ''),
                tgs_data.get('err', ''),
                tgs_data.get('posts_per_week', ''),
                tgs_data.get('category', ''),
                tgs_data.get('language', ''),
                tg_desc,
                tgs_data.get('description', ''),
            ]

            buffer.append((sheet_row, row))

            # Записуємо одразу кожні WRITE_EVERY каналів
            if len(buffer) >= WRITE_EVERY:
                flush_buffer()
                print(f"[{i+1}/{len(channels)}] ✓ записано → {name} | {subs}", flush=True)

        flush_buffer()  # залишки

        print(f"\n✓ ГОТОВО! {len(channels)} каналів, помилок {errors}", flush=True)
        await asyncio.sleep(999999)

asyncio.run(main())
