"""
Аналізує Telegram-канали через OpenRouter і записує результати в Google Sheets.
Для кожного каналу визначає: теги, основний нарратив, вторинні нарративи,
цільову аудиторію (вік, стать, інтереси, стиль).

Запуск:
    python3 06_analyze_channels.py --test   # перші 3 канали
    python3 06_analyze_channels.py           # всі канали з JSON-файлами
"""
import json, os, sys, time
from typing import List
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# ── Налаштування ────────────────────────────────────────────────────
POSTS_DIR = '/Users/r2d2/Documents/dev/openclaw/tgstat_posts'
KEY_FILE  = '/Users/r2d2/Downloads/tg-vk-groups-baa6d186e7f5.json'
SHEET_ID  = '1qnhnIa_1ziZjRICiP4RHBi0OJlF28uyf_3LDPnIMbOA'
TEST_MODE = '--test' in sys.argv

OPENROUTER_API_KEY = 'OPENROUTER_API_KEY_REMOVED'
OPENROUTER_MODEL   = 'google/gemini-2.0-flash-001'

# ── Google Sheets ────────────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds  = Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
gc     = gspread.authorize(creds)
ws     = gc.open_by_key(SHEET_ID).worksheet('Аркуш1')

# ── OpenRouter клієнт ────────────────────────────────────────────────
ai = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url='https://openrouter.ai/api/v1',
)

# ── JSON Schema для структурованого виводу ───────────────────────────
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Теги з дозволеного списку"
        },
        "main_narrative": {
            "type": "string",
            "description": "Основний нарратив каналу (1-3 речення)"
        },
        "secondary_narratives": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Вторинні нарративи (до 3)"
        },
        "audience_age": {"type": "string"},
        "audience_gender": {"type": "string"},
        "audience_life_stage": {"type": "string"},
        "audience_interests": {
            "type": "array",
            "items": {"type": "string"}
        },
        "audience_communication_style": {"type": "string"},
    },
    "required": ["tags", "main_narrative", "secondary_narratives",
                 "audience_age", "audience_gender", "audience_life_stage",
                 "audience_interests", "audience_communication_style"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ти — аналітик Telegram-каналів. Аналізуй контент каналу і надавай структурований аналіз у форматі JSON.

ДОСТУПНІ ТЕГИ — обери всі підходящі:
мамочки/родительство, студенти, жіноча аудиторія, lifestyle, локальне комьюніті,
освітній, медіа, Політика, Екстремізм, Патріотизм, Націоналізм, університетський,
чат/комьюніті, новини, кримінал/НС, розваги, спорт, бізнес, технології, культура,
туризм, реклама, державний/офіційний, регіональний

ОСНОВНИЙ НАРРАТИВ — головна ідея, настрій і модель комунікації каналу (1-3 речення).
ВТОРИННІ НАРРАТИВИ — до 3 додаткових тематичних ліній.
АУДИТОРІЯ: вік (діапазон), стать (переважно чоловіча/жіноча/змішана),
           life_stage (студенти/молоді фахівці/батьки/пенсіонери/тощо),
           інтереси (список), стиль спілкування."""

# ── Читання таблиці ───────────────────────────────────────────────────
print("Читаємо таблицю...", flush=True)
all_rows = ws.get_all_values()
headers  = all_rows[0]
col      = {name: i for i, name in enumerate(headers)}

handle_to_row = {}
for i, row in enumerate(all_rows[1:], start=2):
    link = row[col['Посилання']] if 'Посилання' in col else ''
    handle = link.replace('https://t.me/', '').strip().lower()
    if handle:
        handle_to_row[handle] = i

print(f"Рядків: {len(handle_to_row)}", flush=True)

# ── Нові колонки — знаходимо або створюємо ──────────────────────────
NEW_COLS = ['Теги', 'Основний нарратив', 'Вторинні нарративи',
            'Аудиторія (вік)', 'Аудиторія (стать)', 'Аудиторія (інтереси)',
            'Аудиторія (стиль)']

existing  = ws.row_values(1)
col_idx   = {}

for name in NEW_COLS:
    if name in existing:
        col_idx[name] = existing.index(name) + 1  # 1-indexed
    else:
        pos = len(existing) + 1
        ws.update_cell(1, pos, name)
        existing.append(name)
        col_idx[name] = pos

def col_letter(n: int) -> str:
    result = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(ord('A') + r) + result
    return result

print(f"Колонки аналізу: { {k: col_letter(v) for k,v in col_idx.items()} }", flush=True)

def col_letter(n: int) -> str:
    result = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(ord('A') + r) + result
    return result

# ── Побудова промпту ─────────────────────────────────────────────────
def build_prompt(handle: str, row_data: dict, posts_data: dict) -> str:
    parts = [f"КАНАЛ: @{handle}"]
    for key, label in [('name','Назва'), ('region','Регіон'),
                       ('category','Категорія'), ('language','Мова/Гео'),
                       ('description_tg','Опис (Telegram)'),
                       ('description_tgstat','Опис (TGStat)')]:
        if row_data.get(key):
            parts.append(f"{label}: {row_data[key][:300]}")

    posts = posts_data.get('last_20_posts', [])
    if posts:
        parts.append("\nОСТАННІ ПОСТИ:")
        for i, p in enumerate(posts[:15], 1):
            text = p.get('text', '').strip()[:200]
            if text:
                parts.append(f"{i}. [{p.get('date','')} | {p.get('views','')} переглядів] {text}")

    top = posts_data.get('top_5_by_views', [])
    if top:
        parts.append("\nНАЙПОПУЛЯРНІШІ ПОСТИ:")
        for p in top[:3]:
            text = p.get('text', '').strip()[:200]
            if text:
                parts.append(f"• [{p.get('views','')} переглядів] {text}")

    return '\n'.join(parts)

def get_row_data(row_idx: int) -> dict:
    row = all_rows[row_idx - 1]
    def safe(c): return row[col[c]] if c in col and col[c] < len(row) else ''
    return {
        'name':               safe('Назва'),
        'region':             safe('Регіон'),
        'category':           safe('Категорія'),
        'language':           safe('Мова'),
        'description_tg':     safe('Опис (Telegram)'),
        'description_tgstat': safe('Опис (TGStat)'),
    }

# ── Аналіз через OpenRouter ──────────────────────────────────────────
def analyze(handle: str, row_data: dict, posts_data: dict) -> dict:
    prompt = build_prompt(handle, row_data, posts_data)
    response = ai.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "channel_analysis",
                "schema": ANALYSIS_SCHEMA,
                "strict": True,
            }
        },
        max_tokens=1024,
    )
    return json.loads(response.choices[0].message.content)

# ── Основний цикл ─────────────────────────────────────────────────────
post_files = sorted(f for f in os.listdir(POSTS_DIR)
                    if f.endswith('.json') and not f.startswith('_'))
if TEST_MODE:
    post_files = post_files[:3]
    print(f"⚠ TEST MODE: {len(post_files)} файлів", flush=True)
else:
    print(f"Файлів: {len(post_files)}", flush=True)

ok = errors = 0

for fname in post_files:
    handle  = fname.replace('.json', '').lower()
    row_idx = handle_to_row.get(handle)
    if not row_idx:
        print(f"  @{handle}: не знайдено в таблиці", flush=True)
        continue

    with open(os.path.join(POSTS_DIR, fname)) as f:
        posts_data = json.load(f)

    try:
        a = analyze(handle, get_row_data(row_idx), posts_data)

        tags_str      = ', '.join(a.get('tags', []))
        secondary_str = ' | '.join(a.get('secondary_narratives', []))
        interests_str = ', '.join(a.get('audience_interests', []))

        ws.batch_update([
            {'range': f'{col_letter(col_idx["Теги"])}{row_idx}',                  'values': [[tags_str]]},
            {'range': f'{col_letter(col_idx["Основний нарратив"])}{row_idx}',     'values': [[a.get('main_narrative','')]]},
            {'range': f'{col_letter(col_idx["Вторинні нарративи"])}{row_idx}',    'values': [[secondary_str]]},
            {'range': f'{col_letter(col_idx["Аудиторія (вік)"])}{row_idx}',       'values': [[a.get('audience_age','')]]},
            {'range': f'{col_letter(col_idx["Аудиторія (стать)"])}{row_idx}',     'values': [[a.get('audience_gender','')]]},
            {'range': f'{col_letter(col_idx["Аудиторія (інтереси)"])}{row_idx}',  'values': [[interests_str]]},
            {'range': f'{col_letter(col_idx["Аудиторія (стиль)"])}{row_idx}',     'values': [[a.get('audience_communication_style','')]]},
        ])

        print(f"  ✓ @{handle}: {tags_str[:70]}", flush=True)
        print(f"    {a.get('main_narrative','')[:90]}", flush=True)
        ok += 1

    except Exception as e:
        print(f"  ✗ @{handle}: {e}", flush=True)
        errors += 1

    time.sleep(0.3)

print(f"\n✓ Готово! Оброблено: {ok}, помилок: {errors}")
