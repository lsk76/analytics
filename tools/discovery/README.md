# discovery — Telegram channel/chat resolver

Resolve a t.me link/username to title, type (channel/supergroup/group), subscriber
count and last-post date, using an **authorized Telethon account**. Used to vet and
enroll discussion megagroups for the monitor pipeline.

## ⚠️ Secrets live OUTSIDE the repo

Copying a `.session` or `.env` **into this folder locks the entire project** (the
harness secret-scanner is session-sticky; only a Claude Code restart clears it).

So the Telethon session + Telegram API creds live in **`~/tg-secrets/`**:

```
~/tg-secrets/.env              # TG_API_ID, TG_API_HASH, TG_SESSION=analyzer
~/tg-secrets/analyzer.session  # authorized account "София"
```

Override the location with `TG_SECRETS_DIR=/path/to/secrets`.
`tg_client.py` loads from there; a local `.env` here is for NON-secret overrides only.

## Usage

```bash
cd tools/discovery
python3 tg_fetch_channel.py https://t.me/blogdagchat
```

Needs `telethon` + `python-dotenv` (present in the project `.venv` and system python3).

`scrape_tg_stats.py` is a separate public-page scraper (no Telethon); its Google
service-account JSON also lives outside the repo (`~/Downloads/…json`).
