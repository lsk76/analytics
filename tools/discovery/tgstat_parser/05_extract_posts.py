"""
Витягує пости з збережених HTML сторінок TGStat.
Для кожного каналу зберігає JSON з:
  - останні 20 постів (текст, дата, перегляди, url)
  - топ-5 постів за переглядами
  - дата останнього поста
  - кількість постів за день

Запуск:
    python3 05_extract_posts.py --test    # перші 10 файлів
    python3 05_extract_posts.py           # всі файли
"""
import os, re, sys, json
from bs4 import BeautifulSoup
from collections import Counter

PAGES_DIR = '/Users/r2d2/Documents/dev/openclaw/tgstat_pages'
OUT_DIR   = '/Users/r2d2/Documents/dev/openclaw/tgstat_posts'
TEST_MODE = '--test' in sys.argv

os.makedirs(OUT_DIR, exist_ok=True)

def parse_posts(html):
    soup = BeautifulSoup(html, 'html.parser')
    posts = []

    for container in soup.select('.post-container'):
        # Дата
        date_el = container.select_one('p.text-muted small')
        date_str = date_el.get_text(strip=True) if date_el else ''

        # Текст (всі post-text блоки)
        text_parts = []
        for el in container.select('.post-text'):
            txt = el.get_text(separator=' ', strip=True)
            if txt:
                text_parts.append(txt)
        text = ' '.join(text_parts).strip()

        # URL і ID
        link_el = container.select_one('a[href*="ttttt.me"]')
        post_url = link_el['href'] if link_el else ''
        post_id_m = re.search(r'/(\d+)$', post_url)
        post_id = int(post_id_m.group(1)) if post_id_m else 0

        # Перегляди
        views = 0
        views_link = container.select_one('a[data-src*="/stat"]')
        if views_link:
            raw = views_link.get_text(strip=True)
            digits = re.sub(r'\D', '', raw)
            views = int(digits) if digits else 0

        posts.append({
            'date': date_str,
            'post_id': post_id,
            'url': post_url,
            'views': views,
            'text': text,
        })

    return posts

def analyze(handle, posts):
    if not posts:
        return None

    # Дата останнього поста (перший у списку — найновіший)
    last_date = posts[0]['date']

    # Кількість постів за кожен день
    day_counts = Counter(p['date'].split(',')[0].strip() for p in posts if p['date'])

    # Середня кількість постів за день
    posts_per_day = round(len(posts) / max(len(day_counts), 1), 1)

    # Топ-5 за переглядами
    top_posts = sorted(posts, key=lambda x: x['views'], reverse=True)[:5]

    return {
        'handle': handle,
        'last_post_date': last_date,
        'total_posts_in_html': len(posts),
        'days_covered': len(day_counts),
        'posts_per_day': posts_per_day,
        'posts_by_day': dict(day_counts),
        'last_20_posts': posts[:20],
        'top_5_by_views': top_posts,
    }

files = sorted(f for f in os.listdir(PAGES_DIR) if f.endswith('.html'))
if TEST_MODE:
    files = files[:10]
    print(f"⚠ TEST MODE: {len(files)} файлів")
else:
    print(f"Файлів: {len(files)}")

ok = 0
empty = 0
summary = []  # для зведеного файлу

for fname in files:
    handle = fname.replace('.html', '')
    path = os.path.join(PAGES_DIR, fname)

    with open(path, encoding='utf-8') as f:
        html = f.read()

    posts = parse_posts(html)
    result = analyze(handle, posts)

    if not result:
        empty += 1
        print(f"  @{handle}: порожньо")
        continue

    # Зберігаємо JSON для каналу
    out_path = os.path.join(OUT_DIR, f'{handle}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    summary.append({
        'handle': handle,
        'last_post_date': result['last_post_date'],
        'posts_per_day': result['posts_per_day'],
        'top_post_views': result['top_5_by_views'][0]['views'] if result['top_5_by_views'] else 0,
        'top_post_text': result['top_5_by_views'][0]['text'][:100] if result['top_5_by_views'] else '',
    })

    print(f"  @{handle}: {len(posts)} постів | останній: {result['last_post_date']} | {result['posts_per_day']}/день", flush=True)
    ok += 1

# Зведений файл
summary_path = os.path.join(OUT_DIR, '_summary.json')
with open(summary_path, 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f"\n✓ Готово! Оброблено: {ok}, порожніх: {empty}")
print(f"Файли: {OUT_DIR}")
print(f"Зведений: {summary_path}")
