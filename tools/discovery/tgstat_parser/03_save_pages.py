"""
Завантажує сторінку кожного каналу з TGStat і зберігає HTML
без медіафайлів у папку tgstat_pages/{handle}.html

Запуск:
    python3 03_save_pages.py           # всі канали
    python3 03_save_pages.py --test    # перші 10
    python3 03_save_pages.py --resume  # пропускає вже збережені
"""
import asyncio, json, os, sys, time
from playwright.async_api import async_playwright

RESULTS_FILE = '/tmp/tgstat_results.json'
OUTPUT_DIR   = '/Users/r2d2/Documents/dev/openclaw/tgstat_pages'
PROFILE_DIR  = '/tmp/tgstat_profile'
DELAY        = 2.5   # секунди між запитами

TEST_MODE   = '--test'   in sys.argv
RESUME_MODE = '--resume' in sys.argv

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Збираємо унікальні хендли зі збереженим регіоном
with open(RESULTS_FILE) as f:
    raw = json.load(f)

channels = []
seen = set()
for region, items in raw.items():
    for item in items:
        h = item['handle']
        if h not in seen:
            seen.add(h)
            channels.append({'handle': h, 'region': region})

if TEST_MODE:
    channels = channels[:10]
    print(f"⚠ TEST MODE: {len(channels)} каналів", flush=True)
else:
    print(f"Всього: {len(channels)} каналів", flush=True)

if RESUME_MODE:
    existing = {f.replace('.html', '') for f in os.listdir(OUTPUT_DIR) if f.endswith('.html')}
    before = len(channels)
    channels = [c for c in channels if c['handle'] not in existing]
    print(f"--resume: пропускаємо {before - len(channels)} вже збережених, лишилось {len(channels)}", flush=True)

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel='chrome',
            headless=False,
            args=['--disable-blink-features=AutomationControlled'],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        # Блокуємо медіафайли для швидшого завантаження
        async def block_media(route):
            if route.request.resource_type in ('image', 'media', 'font'):
                await route.abort()
            else:
                await route.continue_()

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.route('**/*', block_media)

        await page.goto('https://tgstat.ru', wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)
        print(">>> Залогінтесь / вирішіть капчу якщо є, потім: touch /tmp/go.flag", flush=True)

        while not os.path.exists('/tmp/go.flag'):
            await asyncio.sleep(1)
        os.remove('/tmp/go.flag')
        print(f"Починаємо! Збереження в {OUTPUT_DIR}\n", flush=True)

        saved = skipped = errors = 0

        for i, ch in enumerate(channels):
            handle = ch['handle']
            out_path = os.path.join(OUTPUT_DIR, f'{handle}.html')

            try:
                await page.goto(
                    f'https://tgstat.ru/channel/@{handle}',
                    wait_until='domcontentloaded',
                    timeout=20000
                )
                # Чекаємо появи блоку статистики (avg_reach, ERR тощо)
                try:
                    await page.wait_for_selector('h2.mb-1.text-dark', timeout=8000)
                except:
                    pass
                await page.wait_for_timeout(4000)

                html = await page.content()

                # Cloudflare або капча — чекаємо поки користувач вирішить
                while 'Just a moment' in html or ('429' in html and 'робота' in html):
                    print(f"  [{i+1}/{len(channels)}] @{handle} — Cloudflare/капча, вирішіть у браузері і натисніть Enter...", flush=True)
                    input()
                    await page.reload(wait_until='domcontentloaded')
                    await page.wait_for_timeout(2000)
                    html = await page.content()

                # Валідна TGStat сторінка: є назва каналу і хоча б якийсь контент
                if len(html) > 50000 or handle.lower() in html.lower():
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(html)
                    saved += 1
                    if (i + 1) % 10 == 0:
                        print(f"  [{i+1}/{len(channels)}] ✓ збережено {saved}, помилок {errors}", flush=True)
                else:
                    skipped += 1
                    print(f"  [{i+1}/{len(channels)}] @{handle} — порожня/заблокована сторінка", flush=True)

            except Exception as e:
                errors += 1
                print(f"  [{i+1}/{len(channels)}] @{handle} — помилка: {e}", flush=True)

            await asyncio.sleep(DELAY)

        print(f"\n✓ ГОТОВО! Збережено: {saved}, пропущено: {skipped}, помилок: {errors}", flush=True)
        print(f"Файли: {OUTPUT_DIR}", flush=True)
        await asyncio.sleep(999999)

asyncio.run(main())
