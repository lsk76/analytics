"""
Collect comments for an opinion-monitor task from TeleZip.

Pipeline:
  1. Read MonitorChat rows (active) for the given task → list of channel tg_ids.
  2. Hit TeleZip `/Find` in 7-day chunks within --from/--to (TeleZip has a
     ~6h internal search timeout for broad terms; chunking is safer).
  3. For each message:
       * keep only chat-messages where ReplyTo is set OR chat is "user-diverse"
         (i.e. no admin dominates no-reply messages) — captured by collecting
         all and post-filtering later in monitor_filter.
       * compute content_hash from text
       * dedup: if (task, author_tg_id, content_hash) exists →
         add chat-username to also_in_chats. Otherwise create new Post.

Examples:
  python manage.py monitor_collect --task dagestan-criticism-monitor \
    --from 2026-05-05 --to 2026-06-04
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as djtz

from analysis.models import AnalysisTask, MonitorChat, Post
from analysis.pilot.logging import open_log
from analysis.services.telezip import TelezipClient


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]


def _parse_dt(s):
    if not s: return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


class Command(BaseCommand):
    help = "Collect comments from TeleZip into Post records for an opinion-monitor task."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True,
                            help="Slug AnalysisTask. Must have active MonitorChat rows.")
        parser.add_argument("--from", dest="date_from", required=True,
                            help="YYYY-MM-DD (UTC).")
        parser.add_argument("--to", dest="date_to", required=True,
                            help="YYYY-MM-DD (UTC).")
        parser.add_argument("--chunk-days", type=int, default=7,
                            help="TeleZip-chunk size in days. Default 7.")
        parser.add_argument("--query", default="",
                            help="Optional TeleZip search query to narrow results "
                                 "to politics-keywords (recommended when active "
                                 "chats include large general-discussion ones).")
        parser.add_argument("--max-text-length", type=int, default=0,
                            help="If >0, skip messages longer than this many chars "
                                 "(rough 'comment vs post' threshold). 0 = keep all.")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        df = date.fromisoformat(opts["date_from"])
        dt = date.fromisoformat(opts["date_to"])
        chunk = max(1, opts["chunk_days"])

        log, log_path = open_log("monitor_collect", task.slug,
                                 extra_tag=f"{df}_{dt}")
        self._log = log
        log(f"log file: {log_path}")

        # 1. resolve whitelist chats
        enrolled = (MonitorChat.objects
                    .filter(task=task, is_active=True)
                    .select_related("channel"))
        chat_ids = [m.channel.tg_id for m in enrolled if m.channel.tg_id]
        chat_by_id = {m.channel.tg_id: m.channel for m in enrolled if m.channel.tg_id}
        if not chat_ids:
            raise CommandError(f"No active MonitorChat for task {task.slug}. "
                               f"Run monitor_enroll first.")
        log(f"== Collect {task.slug} | chats={len(chat_ids)} | "
            f"window={df}→{dt} | chunk={chunk}d ==")
        for cid in chat_ids:
            ch = chat_by_id[cid]
            log(f"  whitelist: id={cid} @{ch.username or '-'}")

        # 2. fetch from TeleZip
        # Дефолт query: спершу беремо CLI-аргумент, потім з AnalysisTask.
        # Так конфіг живе у БД, а CLI може його тимчасово override-нути.
        query = opts["query"] or task.telezip_query or ""
        if query:
            log(f"step 1: TeleZip /Find  query={query[:120]}")
        else:
            log(f"step 1: TeleZip /Find  (no query — усі повідомлення чатів)")
        dfrom = datetime.combine(df, time.min, tzinfo=timezone.utc)
        dto = datetime.combine(dt, time.max, tzinfo=timezone.utc)
        raw = asyncio.run(self._collect(
            query=query,
            dfrom=dfrom, dto=dto, chunk_days=chunk,
            channel_ids=chat_ids, languages=task.languages or ["ru"],
        ))
        log(f"  raw hits: {len(raw)}")

        # 3. ingest — без власної дедуплікації; TeleZip уже згорнув копії
        #    через unique=True. Тому просто bulk_create з ignore_conflicts
        #    (idempotency by (task, url) unique constraint).
        max_len = opts["max_text_length"]
        n_skipped = 0
        log("step 2: ingest (bulk_create)")

        # preload існуючі url'и щоб не питати БД на конфлікти
        existing_urls: set[str] = set(
            Post.objects.filter(task=task).values_list("url", flat=True)
        )
        log(f"  already in DB for this task: {len(existing_urls)} posts")

        to_create: list[Post] = []
        for r in raw:
            text = (r.get("content") or "").strip()
            if not text:
                n_skipped += 1; continue
            if max_len and len(text) > max_len:
                n_skipped += 1; continue
            url = r.get("message_url") or ""
            if not url:
                n_skipped += 1; continue
            if url in existing_urls:
                n_skipped += 1; continue
            ch = chat_by_id.get(r.get("channel_id"))
            if not ch:
                n_skipped += 1; continue

            to_create.append(Post(
                task=task, url=url,
                channel=ch, channel_name=ch.username or f"id{ch.tg_id}",
                posted_at=_parse_dt(r.get("date")),
                telezip_date=_parse_dt(r.get("date")),
                text=text, content_hash=_content_hash(text),
                telezip_mid=r.get("mid"),
                author_name=r.get("from_user_name") or "",
                author_tg_id=r.get("from_user_id"),
                reply_to_msg=r.get("reply_to"),
                classification={"_monitor": True, "_collect_source": "monitor_collect"},
            ))

        if to_create:
            log(f"  bulk_create starting: {len(to_create)} new posts ...")
            Post.objects.bulk_create(to_create, batch_size=1000,
                                     ignore_conflicts=True)
            log(f"  bulk_create done")

        log(f"DONE: saved {len(to_create)} new | "
            f"skipped {n_skipped} (empty/too-long/already-in-DB)")

    async def _collect(self, query: str, dfrom: datetime, dto: datetime,
                       chunk_days: int, channel_ids: List[int],
                       languages: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_urls = set()
        async with TelezipClient(settings.TELEZIP_API_KEY,
                                 settings.TELEZIP_BASE_URL) as tz:
            cur = dfrom
            i = 0
            step = timedelta(days=chunk_days)
            while cur < dto:
                nxt = min(cur + step, dto)
                i += 1
                try:
                    batch = await tz.find_posts(
                        query, cur, nxt, languages, unique=True,
                        channel_ids=channel_ids,
                    )
                except Exception as e:
                    self._log(f"  chunk {i} {cur.date()}→{nxt.date()} FAILED: {e}")
                    cur = nxt
                    continue
                new = 0
                for r in batch:
                    u = r.get("message_url")
                    if u and u in seen_urls:
                        continue
                    if u: seen_urls.add(u)
                    out.append(r)
                    new += 1
                self._log(
                    f"  chunk {i:>2} {cur.date()}→{nxt.date()}: "
                    f"+{new} (raw {len(batch)})"
                )
                cur = nxt
        return out
