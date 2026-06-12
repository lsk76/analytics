import asyncio, json, os, re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

PROFILE_DIR  = '/tmp/tgstat_profile'
RESULTS_FILE = '/tmp/tgstat_results.json'

QUERIES = [
    ('Бурятія',      'Бурятия'),
    ('Саха',         'Якутия'),
    ('Тива',         'Тыва'),
    ('Татарстан',    'Татарстан'),
    ('Башкортостан', 'Башкортостан'),
    ('Чечня',        'Чечня'),
    ('Чечня',        'Ичкерия'),
    ('Інгушетія',    'Ингушетия'),
    ('Дагестан',     'Дагестан'),
]

FETCH_JS = """
async ({query, page, offset}) => {
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    const p = new URLSearchParams();
    p.set('_tgstat_csrk', csrf);
    p.set('view', 'list');
    p.set('sort', 'participants');
    p.set('q', query);
    p.set('inAbout', '0');
    p.append('countries[]', '1');
    ['categories','languages','channelType','participantsCountTo',
     'avgReachFrom','avgReachTo','avgReach24From','avgReach24To','ciFrom','ciTo'].forEach(k => p.set(k,''));
    p.set('participantsCountFrom', '800');
    p.set('age', '0-120'); p.set('err', '0-100'); p.set('er','0');
    p.set('male','0'); p.set('female','0');
    p.set('isVerified','0'); p.set('isRknVerified','0'); p.set('isStoriesAvailable','0');
    ['noRedLabel','noScam','noDead'].forEach(k => { p.append(k,'0'); p.append(k,'1'); });
    p.set('page', String(page));
    p.set('offset', String(offset));

    const r = await fetch('/channels/search', {
        method: 'POST',
        headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With':'XMLHttpRequest'},
        body: p.toString(),
        credentials: 'include',
    });
    if (!r.ok) return {error: r.status};
    return await r.json();
}
"""

def parse_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    channels = []
    seen = set()
    for item in soup.select('div.col-12, div[class*="peer"], div[class*="channel"]'):
        link = item.select_one('a[href*="/channel/@"]')
        if not link:
            continue
        m = re.search(r'/channel/@(\w+)', link['href'])
        if not m:
            continue
        handle = m.group(1)
        if handle in seen:
            continue
        seen.add(handle)
        name_el = item.select_one('[class*="name"], [class*="title"], b')
        name = name_el.get_text(strip=True) if name_el else ''
        subs_el = item.select_one('[class*="participants"], [class*="members"], [class*="counter"]')
        subs = subs_el.get_text(strip=True) if subs_el else ''
        channels.append({'handle': handle, 'name': name, 'subs': subs})
    return channels

async def scrape_query(browser_page, query):
    channels, seen = [], set()
    page_num, offset = 0, 0

    while True:
        data = await browser_page.evaluate(FETCH_JS, {'query': query, 'page': page_num, 'offset': offset})

        if 'error' in data:
            print(f"  ⚠ {data['error']}", flush=True)
            break
        if data.get('status') != 'ok':
            print(f"  ⚠ {data}", flush=True)
            break

        chunk = parse_html(data.get('html', ''))
        new = [c for c in chunk if c['handle'] not in seen]
        for c in new: seen.add(c['handle'])
        channels.extend(new)

        next_page   = data.get('nextPage', page_num + 1)
        next_offset = data.get('nextOffset', offset + 30)
        has_more    = data.get('hasMore', False)

        print(f"  page={page_num} offset={offset}: +{len(new)} нових, всього={len(channels)}, hasMore={has_more}", flush=True)

        if not has_more or not new:
            break

        page_num = next_page
        offset   = next_offset
        await asyncio.sleep(1.5)

    return channels

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
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto('https://tgstat.ru/channels/search', wait_until='domcontentloaded')
        await page.wait_for_timeout(2000)

        print(">>> Залогінтесь якщо потрібно, потім: touch /tmp/go.flag", flush=True)
        while not os.path.exists('/tmp/go.flag'):
            await asyncio.sleep(1)
        os.remove('/tmp/go.flag')
        print("Старт! підписники > 800\n", flush=True)

        results = {}
        for region, query in QUERIES:
            print(f"\n=== {region}: «{query}» ===", flush=True)
            channels = await scrape_query(page, query)
            existing = {c['handle'] for c in results.get(region, [])}
            results.setdefault(region, []).extend(
                c for c in channels if c['handle'] not in existing
            )
            print(f"  Разом {region}: {len(results[region])}", flush=True)
            with open(RESULTS_FILE, 'w') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            await asyncio.sleep(2)

        total = sum(len(v) for v in results.values())
        print(f"\n✓ Готово! {total} каналів → {RESULTS_FILE}", flush=True)
        await asyncio.sleep(999999)

asyncio.run(main())
