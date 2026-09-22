"""Довідник каналів і джерел — бекфіл (ідемпотентний, можна ганяти повторно).

1. Channel.url/platform для всіх Telegram-рядків (services/directory.py):
   юзернейм → https://t.me/<name>; у дублях за регістром юзернейм лишає
   найсвіжіший (fetched_at/id), решта — за tg_id (https://t.me/c/<id>).
2. Source.channel: Telegram-джерело → рядок довідника за юзернеймом (або
   створюється), сайт/RSS → новий рядок довідника з platform web/rss.
3. Пости інформпростору з Telegram-джерел отримують channel (для охоплення).
4. Події цих досліджень: перерахунок channel_count/reach.

  python manage.py directory_backfill [--dry-run]
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from analysis.models import AnalysisTask, Channel, Event, Post, Source
from analysis.services import directory as d


class Command(BaseCommand):
    help = "Бекфіл довідника: Channel.url/platform, Source.channel, Post.channel, reach."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        with transaction.atomic():
            n_ch = self._channels_url()
            n_src, n_new, n_miss = self._sources()
            n_posts = self._posts()
            n_ev = self._reach()
            dup = self._url_conflicts()
            self.stdout.write(
                f"channels url set: {n_ch}; sources linked: {n_src} (new directory rows: {n_new}, "
                f"unresolved: {n_miss}); posts got channel: {n_posts}; events recomputed: {n_ev}; "
                f"url conflicts: {len(dup)}")
            for u, ids in dup[:20]:
                self.stdout.write(f"  CONFLICT {u}: channel ids {ids}")
            if dry:
                transaction.set_rollback(True)
                self.stdout.write("dry-run: відкочено")

    # ------------------------------------------------------------------ 1
    def _channels_url(self) -> int:
        # найсвіжіший рядок групи (за юзернеймом без регістру) забирає адресу з юзернеймом
        groups = defaultdict(list)
        for c in Channel.objects.filter(platform="telegram").exclude(username="") \
                .only("id", "username", "tg_id", "fetched_at").order_by("-fetched_at", "-id"):
            groups[c.username.lower()].append(c)
        winners = {cs[0].id for cs in groups.values()}
        todo, stubs = [], []
        for c in Channel.objects.filter(platform="telegram").only("id", "username", "tg_id", "url"):
            fields = ["url"]
            if c.tg_id is None and re.match(r"^-?\d{6,}$", c.username or ""):
                # tg_id, записаний у юзернейм: зберігаємо у внутрішній формі (без -100),
                # як у решті довідника; якщо такий tg_id уже є — це дубль-стаб
                internal = d.tg_internal_id(int(c.username))
                if Channel.objects.filter(tg_id=internal).exclude(id=c.id).exists():
                    stubs.append(c)
                    continue
                c.tg_id, c.username = internal, ""
                fields += ["tg_id", "username"]
            _, url = d.channel_url(c) if c.id in winners or not c.username else (
                "telegram", d.telegram_url("", c.tg_id))
            if not url:
                stubs.append(c)          # дубль за юзернеймом без tg_id — злити вручну
                continue
            if url != c.url or len(fields) > 1:
                c.url = url
                todo.append(c)
        Channel.objects.bulk_update(todo, ["url", "tg_id", "username"], batch_size=2000)
        for c in stubs:
            self.stderr.write(f"  channel #{c.id} «{c.username}»: дубль без tg_id, адресу не дано — злити вручну")
        return len(todo)

    # ------------------------------------------------------------------ 2
    def _sources(self):
        """Звʼязати джерела з довідником. Працює у ПРОМІЖНОМУ стані БД (після
        0084, до 0085): колонки name/url/region_subject_id/language ще є в
        таблиці, але вже не в моделі — читаємо їх сирим SQL. Після 0085 колонок
        нема, джерело без довідника неможливе — лише звіт."""
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='analysis_source' AND column_name='url'")
            legacy = cur.fetchone() is not None
        if not legacy:
            return 0, 0, Source.objects.filter(channel__isnull=True).count()
        with connection.cursor() as cur:
            cur.execute("SELECT id, kind, url, name, region_subject_id, language "
                        "FROM analysis_source WHERE channel_id IS NULL ORDER BY id")
            rows = cur.fetchall()
        linked = new = missing = 0
        by_url = {}
        for sid, kind, raw_url, name, region_id, language in rows:
            platform, url = d.normalize_url(raw_url or "", kind_hint=kind)
            if not url:
                missing += 1
                self.stderr.write(f"  source #{sid} {kind} «{raw_url}»: не нормалізується")
                continue
            ch = by_url.get(url) or Channel.objects.filter(url=url).order_by("-fetched_at", "-id").first()
            if ch is None and platform == "telegram":
                m = re.match(r"^https://t\.me/([a-z0-9_]+)$", url)
                if m:
                    ch = Channel.objects.filter(username__iexact=m.group(1)).order_by("-fetched_at", "-id").first()
            if ch is None:
                ch = Channel.objects.create(
                    platform=platform, url=url, title=(name or "")[:512],
                    region_subject_id=region_id, language=language or "",
                    username=(url.removeprefix("https://t.me/") if platform == "telegram"
                              and not url.startswith(("https://t.me/c/", "https://t.me/+")) else ""),
                    chat_type="channel" if platform == "telegram" else "",
                )
                new += 1
            else:
                changed = []
                if not ch.url:
                    ch.url, changed = url, changed + ["url"]
                if ch.region_subject_id is None and region_id:
                    ch.region_subject_id, changed = region_id, changed + ["region_subject"]
                if changed:
                    ch.save(update_fields=changed)
            by_url[url] = ch
            Source.objects.filter(id=sid).update(channel=ch)
            linked += 1
        return linked, new, missing

    # ------------------------------------------------------------------ 3
    def _posts(self) -> int:
        n = 0
        for src in Source.objects.filter(kind="telegram", channel__isnull=False).only("id", "channel_id"):
            n += Post.objects.filter(source_id=src.id, channel__isnull=True).update(channel_id=src.channel_id)
        return n

    # ------------------------------------------------------------------ 4
    def _reach(self) -> int:
        n = 0
        tasks = AnalysisTask.objects.filter(pipeline=AnalysisTask.PIPELINE_INFOSPACE)
        for ev in Event.objects.filter(task__in=tasks).only("id", "post_count", "channel_count", "reach").iterator(chunk_size=2000):
            chans = {}
            for cid, subs in Post.objects.filter(event_id=ev.id, channel__isnull=False) \
                    .values_list("channel_id", "channel__subscribers"):
                chans[cid] = subs or 0
            cc, reach = len(chans), sum(chans.values())
            if cc != ev.channel_count or reach != ev.reach:
                Event.objects.filter(id=ev.id).update(channel_count=cc, reach=reach)
                n += 1
        return n

    # ------------------------------------------------------------------ check
    def _url_conflicts(self):
        rows = (Channel.objects.exclude(url="").order_by().values("url")
                .annotate(n=Count("id")).filter(n__gt=1))
        return [(r["url"], list(Channel.objects.filter(url=r["url"]).values_list("id", flat=True)))
                for r in rows]
