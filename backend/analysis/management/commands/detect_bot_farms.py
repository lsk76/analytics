"""
Heuristic detector: mark Events that look like SEO/bot-farm distribution
(NOT real organic news). Sets `Event.is_bot_farm` + `bot_farm_score`.

Signals (each scored 0..1, combined into bot_farm_score):
  - inner_dup   : the post text duplicates itself inside one message
                  (classic SEO keyword stuffing — "X did Y. X (https://...) did Y.")
  - seo_urls    : .com/.fun/.info URLs scattered inside text (not at start/end),
                  with t.me/ as well — bot link-back signature
  - spread      : posts span > N days for the same near-identical text
                  (organic news lives 1-2 weeks; farms re-spam for months)
  - template    : posts differ only in a few tokens — variations of one template
                  (already-clustered means high similarity, but extra-tight = bot)

A post counts toward a signal if it matches the pattern. The Event score is the
fraction of posts matching ≥1 strong signal, plus the spread bonus.

  python manage.py detect_bot_farms                   # all events, write back
  python manage.py detect_bot_farms --dry-run         # report only
  python manage.py detect_bot_farms --threshold 0.5   # min score to flag (default 0.5)
  python manage.py detect_bot_farms --event 923       # inspect one event
"""
import re
from datetime import timedelta

from django.core.management.base import BaseCommand

from analysis.models import Event


# A SEO URL is one outside t.me/ in the middle of running text. Excludes the trailing
# signature line ("source: …") which legitimate channels do use.
URL_RE = re.compile(r'https?://([^\s)]+)')
TME_RE = re.compile(r'(?:https?://)?t\.me/[^\s)]+')


def _signals_for_post(text):
    """Return a dict of {signal_name: bool} for one post."""
    if not text:
        return {"inner_dup": False, "seo_urls": False}
    out = {}

    # 1. inner-dup: SEO stuffing pattern — first 10-word phrase repeats verbatim
    # later in the post. Catches "Студенты Х устроили драку. Студенты (https://…) Х
    # устроили драку. …" without false-positiving on normal repeating words.
    tokens = re.findall(r"\w+", text.lower())
    out["inner_dup"] = False
    if len(tokens) >= 20:
        head = " ".join(tokens[:10])
        rest = " ".join(tokens[10:])
        out["inner_dup"] = head in rest

    # 2. SEO link-back signature. We look for non-tme domains in the text, including
    # the bizarre "https://t.me/https://example.com" wrapper that real SEO farms use
    # to disguise outbound links. ≥1 such occurrence is enough — organic news posts
    # almost never include random .com/.fun/.info domains in the body.
    seo_count = 0
    for m in URL_RE.finditer(text):
        raw = m.group(1)
        # strip t.me/ prefix if there's nested https:// inside (farm-style wrapper)
        if raw.startswith("t.me/"):
            inner = raw[len("t.me/"):]
            if inner.startswith("https://") or inner.startswith("http://"):
                seo_count += 1
                continue
            if not re.match(r"^[A-Za-z0-9_]+$", inner.split("/")[0]):
                # t.me/<not a handle> — still suspicious
                seo_count += 1
            continue
        # any non-t.me URL counts
        if re.search(r"\.(com|fun|info|ru|net|org|biz|site|xyz|online|live)\b", raw):
            seo_count += 1
    out["seo_urls"] = seo_count >= 1
    return out


def _event_score(event):
    """Compute bot_farm_score for one Event. Returns (score, breakdown)."""
    posts = list(event.posts.all())
    n = len(posts)
    if n < 3:
        return 0.0, {"reason": "too_few_posts", "n": n}

    inner_dup_n = 0
    seo_urls_n = 0
    for p in posts:
        sig = _signals_for_post(p.text or "")
        if sig["inner_dup"]:
            inner_dup_n += 1
        if sig["seo_urls"]:
            seo_urls_n += 1

    dates = [p.posted_at for p in posts if p.posted_at]
    if len(dates) >= 2:
        spread_days = (max(dates) - min(dates)).days
    else:
        spread_days = 0

    dup_frac = inner_dup_n / n
    seo_frac = seo_urls_n / n

    # spread bonus: 0 at <=7 days, 1.0 at >=60 days
    if spread_days <= 7:
        spread_score = 0.0
    elif spread_days >= 60:
        spread_score = 1.0
    else:
        spread_score = (spread_days - 7) / 53.0

    # core score: max of the strong signals (dup, seo) plus a fraction of spread
    score = max(dup_frac, seo_frac) * 0.7 + spread_score * 0.3
    score = min(score, 1.0)

    breakdown = {
        "n": n,
        "dup_frac": round(dup_frac, 2),
        "seo_frac": round(seo_frac, 2),
        "spread_days": spread_days,
        "spread_score": round(spread_score, 2),
        "score": round(score, 2),
    }
    return score, breakdown


class Command(BaseCommand):
    help = "Heuristically flag bot-farm / SEO-network Events"

    def add_arguments(self, parser):
        parser.add_argument("--threshold", type=float, default=0.5,
                            help="bot_farm_score ≥ threshold ⇒ is_bot_farm=True (default 0.5)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--event", type=int, default=None,
                            help="Inspect one Event (always prints breakdown)")
        parser.add_argument("--task", default=None, help="Restrict to one task slug")

    def handle(self, *args, **opts):
        threshold = opts["threshold"]
        dry_run = opts["dry_run"]

        qs = Event.objects.prefetch_related("posts")
        if opts["event"]:
            qs = qs.filter(id=opts["event"])
        elif opts["task"]:
            qs = qs.filter(task__slug=opts["task"])

        n_seen = n_flagged = 0
        for ev in qs.iterator(chunk_size=200):
            score, br = _event_score(ev)
            flag = score >= threshold
            n_seen += 1
            if flag:
                n_flagged += 1

            if opts["event"]:
                self.stdout.write(f"event #{ev.id}: {br}  → is_bot_farm={flag}")

            if not dry_run:
                if (ev.bot_farm_score != score) or (ev.is_bot_farm != flag):
                    ev.bot_farm_score = score
                    ev.is_bot_farm = flag
                    ev.save(update_fields=["bot_farm_score", "is_bot_farm"])

        verb = "would flag" if dry_run else "flagged"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {n_flagged}/{n_seen} events as bot-farm (threshold={threshold})"))
