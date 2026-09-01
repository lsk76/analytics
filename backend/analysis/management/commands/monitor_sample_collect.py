"""
Вибірковий збір коментарів через Telethon (на відміну від monitor_collect, який
тягне суцільний потік із TeleZip).

Навіщо вибірка: суцільний збір по 20 регіонах — сотні тисяч повідомлень на місяць,
що не окупається. Для метрики «частка критики» достатньо випадкової вибірки:
1500 повідомлень на регіон дають похибку ±1 в.п. при частці ~4%.

Як влаштовано:
  1. межі періоду -> id першого й останнього повідомлення (offset_date, 2 запити);
  2. квота регіону ділиться між його чатами ПРОПОРЦІЙНО обсягу (діапазону id),
     інакше маленький чат отримає непропорційну вагу в регіональній частці;
  3. з діапазону береться N випадкових id і читається пачками по 200
     (get_messages(ids=[...])) — це справжня випадкова вибірка, а не «останні N»;
  4. пишеться MonitorSample — паспорт вибірки, з якого рахується знаменник.

Чому саме id, а не гортання по датах: id адресуються напряму, тож вибірка
рівномірна по всьому періоду і коштує 1 запит на 200 повідомлень. Побічно
діапазон id дає оцінку обсягу чату — окремо рахувати знаменник не треба.

ВАЖЛИВО: кожен чат читається ТИМ акаунтом, що записаний у MonitorChat.tg_account —
резолв юзернейма має добовий ліміт (~200/акаунт) і кешується в сесії того акаунта.

Приклади:
  python manage.py monitor_sample_collect --task fedcrit-sib-dv \
      --from 2026-07-01 --to 2026-07-31 --per-region 1500 --dry-run
  python manage.py monitor_sample_collect --task fedcrit-sib-dv \
      --from 2026-07-01 --to 2026-07-31 --regions "Тива,Хакасія"
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import date, datetime, time, timedelta, timezone

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from analysis.models import AnalysisTask, MonitorChat, MonitorSample, Post

BATCH = 200          # стеля get_messages(ids=[...]) за один запит
PAUSE = 1.5          # пауза між запитами до Telegram


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]


def _url(channel, mid: int) -> str:
    """Публічний юзернейм -> t.me/<name>/<id>; приватна група -> t.me/c/<internal>/<id>."""
    u = (channel.username or "").strip()
    if u and not u.startswith(("linked:", "+")):
        return f"https://t.me/{u}/{mid}"
    internal = str(abs(channel.tg_id or 0)).removeprefix("100")
    return f"https://t.me/c/{internal}/{mid}"


class Command(BaseCommand):
    help = "Випадкова вибірка коментарів за період через Telethon (monitor-задача)."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True, help="slug monitor-задачі")
        parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (UTC)")
        parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD (UTC), включно")
        parser.add_argument("--per-region", type=int, default=1500,
                            help="Цільова вибірка на регіон. Менше за обсяг регіону — "
                                 "беремо все, що є. Дефолт 1500 (±1 в.п.).")
        parser.add_argument("--regions", default="", help="Лише ці регіони (кома)")
        parser.add_argument("--seed", type=int, default=0,
                            help="Зерно ГВЧ — щоб вибірку можна було відтворити.")
        parser.add_argument("--probe", type=int, default=0,
                            help="Режим перевірки джерел: взяти N випадкових id У КОЖНОМУ "
                                 "чаті, порахувати частку живих людей і НЕ писати пости. "
                                 "Потрібен, бо «повідомлень/добу» рахує й автопересилки "
                                 "каналу — група новинного каналу з нулем коментарів "
                                 "виглядає жвавою.")
        parser.add_argument("--resume", action="store_true",
                            help="Пропустити чати, для яких вибірка за цей період уже є "
                                 "(відновлення після обриву).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Порахувати межі й квоти, нічого не читати й не писати.")

    def handle(self, *args, **o):
        try:
            task = AnalysisTask.objects.get(slug=o["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Задачі {o['task']!r} немає.")
        d_from, d_to = date.fromisoformat(o["date_from"]), date.fromisoformat(o["date_to"])
        if d_to < d_from:
            raise CommandError("--to раніше за --from")
        only = [r.strip() for r in o["regions"].split(",") if r.strip()]

        # proxy теж select_related: у async-контексті лінива підвантажка FK падає
        chats = (MonitorChat.objects.filter(task=task, is_active=True)
                 .select_related("channel", "channel__region_subject",
                                 "tg_account", "tg_account__proxy")
                 .order_by("channel__region_subject__name", "priority"))
        if only:
            chats = chats.filter(channel__region_subject__name__in=only)
        chats = [c for c in chats if c.tg_account and c.tg_account.is_authenticated]
        if not chats:
            raise CommandError("Немає активних чатів з прив'язаним авторизованим акаунтом.")

        by_region: dict[str, list] = {}
        for c in chats:
            by_region.setdefault(c.channel.region_subject.name, []).append(c)
        self.stdout.write(f"задача {task.slug}: {len(chats)} чатів у {len(by_region)} регіонах, "
                          f"період {d_from}..{d_to}, квота {o['per_region']}/регіон")

        # частка живих людей у потоці кожного чату — з попереднього заміру.
        # Рахуємо ТУТ (синхронно): у async-гілці Django ORM недоступний.
        yields = {}
        if o["resume"]:
            # Ознака «вже зібрано» — саме ПОСТИ за період, а не MonitorSample:
            # розвідувальний прогін (--probe) теж пише MonitorSample у це вікно,
            # і за ним чат виглядав би зібраним, хоча постів немає.
            done_ids = set(Post.objects.filter(
                task=task, posted_at__date__gte=d_from, posted_at__date__lte=d_to)
                .values_list("channel_id", flat=True).distinct())
            before = len(chats)
            chats = [c for c in chats if c.channel_id not in done_ids]
            by_region = {}
            for c in chats:
                by_region.setdefault(c.channel.region_subject.name, []).append(c)
            self.stdout.write(f"--resume: пропущено вже зібраних {before - len(chats)}, "
                              f"лишилось {len(chats)}")
            if not chats:
                return

        for c in chats:
            prev = (MonitorSample.objects.filter(channel=c.channel)
                    .order_by("-period_start").first())
            y = (prev.n_user / prev.n_requested) if prev and prev.n_requested else 1.0
            yields[c.channel_id] = max(y, 0.02)       # підлога: не ділити на ~0

        rnd = random.Random(o["seed"] or None)
        asyncio.run(self._run(task, by_region, d_from, d_to, o["per_region"],
                              rnd, o["dry_run"], o["probe"], yields))

    # ---------- резолв чату ----------

    async def _resolve(self, client, channel):
        """Entity чату. Сирий tg_id НЕ годиться: без access_hash у сесії Telethon
        приймає його за користувача. Тому:
          linked:<канал> -> резолв каналу -> його linked_chat (hash приходить разом);
          +<інвайт>      -> CheckChatInvite (працює, якщо акаунт уже учасник);
          <юзернейм>     -> напряму."""
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import CheckChatInviteRequest

        u = (channel.username or "").strip()
        if u.startswith("linked:"):
            ent = await client.get_entity(u.split(":", 1)[1])
            full = await client(GetFullChannelRequest(ent))
            cid = getattr(full.full_chat, "linked_chat_id", None)
            if not cid:
                raise RuntimeError("у каналу немає групи обговорень")
            return await client.get_entity(cid)
        if u.startswith("+"):
            info = await client(CheckChatInviteRequest(u.lstrip("+")))
            chat = getattr(info, "chat", None)
            if chat is None:
                raise RuntimeError("акаунт не учасник закритої групи")
            return chat
        if u:
            ent = await client.get_entity(u)
            # Юзернейм може вказувати на КАНАЛ (мовлення), а люди пишуть у його групі
            # обговорень. Без цієї перевірки збирач читав би пости редакції — 0% людей.
            if getattr(ent, "broadcast", False):
                full = await client(GetFullChannelRequest(ent))
                linked = getattr(full.full_chat, "linked_chat_id", None)
                if linked:
                    return await client.get_entity(linked)
            return ent
        raise RuntimeError("немає юзернейма для резолву")

    # ---------- межі періоду ----------

    async def _bounds(self, client, entity, d_from: date, d_to: date):
        """id першого повідомлення від d_from і останнього до кінця d_to."""
        lo = await client.get_messages(
            entity, limit=1, offset_date=datetime.combine(d_from, time.min, timezone.utc),
            reverse=True)                                  # найстаріше ПІСЛЯ дати
        hi = await client.get_messages(
            entity, limit=1,
            offset_date=datetime.combine(d_to, time.max, timezone.utc))  # найновіше ДО дати
        if not lo or not hi or hi[0].id <= lo[0].id:
            return None
        return lo[0].id, hi[0].id

    # ---------- основний прохід ----------

    async def _run(self, task, by_region, d_from, d_to, per_region, rnd, dry, probe, yields):
        # один клієнт на акаунт: реконект = новий IP для тієї ж сесії
        accounts = {c.tg_account_id: c.tg_account
                    for lst in by_region.values() for c in lst}
        clients = {}
        for aid, acc in accounts.items():
            clients[aid] = TelegramClient(
                StringSession(acc.session_string), int(acc.api_id), acc.api_hash,
                proxy=acc.proxy.to_telethon_proxy() if acc.proxy else None,
                **acc.client_kwargs())
            await clients[aid].connect()
        try:
            for region, lst in by_region.items():
                await self._region(task, region, lst, clients, d_from, d_to,
                                   per_region, rnd, dry, probe, yields)
        finally:
            # КРИТИЧНО: Telethon кешує access_hash розв'язаних чатів У СЕСІЇ. Якщо не
            # зберегти її назад у БД, наступний прогін резолвитиме все наново і виб'є
            # добовий ліміт ResolveUsername (~200/акаунт -> FloodWait на години).
            @sync_to_async(thread_sensitive=True)
            def _save_sessions():
                for aid, acc in accounts.items():
                    acc.session_string = clients[aid].session.save()
                    acc.save(update_fields=["session_string"])

            for cl in clients.values():
                await cl.disconnect()
            await _save_sessions()
            self.stdout.write(f"сесії {len(accounts)} акаунтів збережено "
                              f"(кеш entity переживе наступний прогін)")

    async def _region(self, task, region, chats, clients, d_from, d_to,
                      per_region, rnd, dry, probe, yields):
        spans = []
        for mc in chats:
            client = clients[mc.tg_account_id]
            try:
                ent = await self._resolve(client, mc.channel)
                b = await self._bounds(client, ent, d_from, d_to)
            except FloodWaitError as e:
                self.stdout.write(self.style.WARNING(
                    f"  {region}: FloodWait {e.seconds}s на {mc.channel.username} — регіон пропущено"))
                return
            except Exception as e:  # noqa: BLE001 — недоступний чат не має валити регіон
                self.stdout.write(f"  {mc.channel.username}: {type(e).__name__}: {str(e)[:60]}")
                continue
            if b:
                spans.append((mc, ent, b[0], b[1]))
            await asyncio.sleep(PAUSE)

        # Квоту ділимо за ЛЮДСЬКИМ обсягом, не за діапазоном id: у діапазон входять
        # автопересилки каналу, і без поправки на них вибірка людських коментарів
        # виходить меншою за цільову (у деяких чатах — у 5-10 разів).
        total = sum(hi - lo for _, _, lo, hi in spans)
        human_total = sum((hi - lo) * yields[mc.channel_id] for mc, _, lo, hi in spans)
        if not total:
            self.stdout.write(f"  {region}: {'жоден чат не відкрився' if not spans else 'повідомлень за період немає'}")
            return
        target = min(per_region, int(human_total))
        self.stdout.write(f"\n{region}: ~{total:,} id, з них людських ~{human_total:,.0f}; "
                          f"ціль {target:,} людських")

        for mc, ent, lo, hi in spans:
            span = hi - lo
            y = yields[mc.channel_id]
            if probe:
                n = probe
            else:
                humans_i = target * (span * y) / human_total if human_total else 0
                n = max(1, round(humans_i / y))       # id, щоб набрати humans_i людських
            n = min(n, span)
            self.stdout.write(f"  {mc.channel.username[:32]:<34}діапазон {span:>7,} × "
                              f"{y*100:>3.0f}% людей -> беремо {n:>5,} id")
            if dry:
                continue
            await self._sample_chat(task, mc, ent, clients[mc.tg_account_id],
                                    lo, hi, n, d_from, d_to, rnd, probe=bool(probe))

    # ---------- вибірка одного чату ----------

    async def _sample_chat(self, task, mc, entity, client, lo, hi, n, d_from, d_to,
                           rnd, probe=False):
        ids = rnd.sample(range(lo, hi + 1), n)
        ch = mc.channel
        n_returned = n_text = n_user = 0
        posts = []
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            try:
                if not client.is_connected():          # мережа могла впасти між чатами
                    await client.connect()
                msgs = await client.get_messages(entity, ids=chunk)
            except FloodWaitError as e:
                self.stdout.write(self.style.WARNING(
                    f"    FloodWait {e.seconds}s — зупинка на {n_returned} повідомленнях"))
                break
            except Exception as e:  # noqa: BLE001 — обрив мережі не має валити весь прогін
                self.stdout.write(self.style.WARNING(
                    f"    {type(e).__name__}: {str(e)[:70]} — чат обірвано на {n_returned}"))
                break
            for m in msgs:
                if m is None:            # видалене / службове / неіснуючий id
                    continue
                n_returned += 1
                text = (getattr(m, "message", None) or "").strip()
                if not text:
                    continue
                n_text += 1
                # Відправник із мінусом (-100…) — це САМ КАНАЛ автопересилає свій пост
                # у групу обговорень як анкер. Такі повідомлення не є думкою людини:
                # у групі новинного каналу вони можуть давати 99% потоку.
                sender = getattr(m, "sender_id", None)
                is_repost = not isinstance(sender, int) or sender < 0
                if not is_repost:
                    n_user += 1
                posts.append(Post(
                    is_channel_repost=is_repost,
                    task=task, url=_url(ch, m.id), channel=ch,
                    channel_name=ch.username or f"id{ch.tg_id}",
                    region_subject_id=ch.region_subject_id,   # денормалізація, як у mon_collect
                    posted_at=m.date.astimezone(timezone.utc) if m.date else None,
                    text=text, content_hash=_hash(text),
                    author_tg_id=sender if isinstance(sender, int) else None,
                    reply_to_msg=getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None),
                    stage=Post.STAGE_MON_COLLECTED,
                    classification={"_monitor": True, "_collect_source": "sample",
                                    "_sample_period": f"{d_from}..{d_to}"},
                ))
            await asyncio.sleep(PAUSE)

        # ORM у async-контексті — лише через sync_to_async
        @sync_to_async(thread_sensitive=True)
        def _persist():
            if posts and not probe:          # у режимі перевірки пости не пишемо
                Post.objects.bulk_create(posts, batch_size=1000, ignore_conflicts=True)
            MonitorSample.objects.update_or_create(
                task=task, channel=ch, period_start=d_from,
                defaults={"period_end": d_to, "id_lo": lo, "id_hi": hi,
                          "n_requested": n, "n_returned": n_returned,
                          "n_text": n_text, "n_user": n_user})

        await _persist()
        est_user = int(round((hi - lo) * n_user / n)) if n else 0
        rate = (n_user / n_text * 100) if n_text else 0
        flag = "  ⚠ РУПОР, не чат" if rate < 20 else ""
        self.stdout.write(f"    {'перевірка' if probe else f'збережено {len(posts):>5,}'} | текст {n_text}/{n} | "
                          f"від людей {n_user} ({rate:.0f}%) | людських за період ~{est_user:,}{flag}")
