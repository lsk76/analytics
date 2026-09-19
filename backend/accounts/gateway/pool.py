"""AccountPool — {account_id: LiveAccount} у процесі gateway + фонові цикли:
ремонт (repair.py), idle-відключення, періодичне закриття DB-зʼєднань."""
from __future__ import annotations

import asyncio
import logging
import time

from asgiref.sync import sync_to_async

from . import repair as rp
from .live import LiveAccount

logger = logging.getLogger("accounts.gateway.pool")


def _setting_int(key: str, default: int) -> int:
    from analysis.models import Setting
    try:
        return int(Setting.get(key, default))
    except (TypeError, ValueError):
        return default


class AccountPool:
    def __init__(self):
        self.live: dict[int, LiveAccount] = {}
        self._repair_queue: asyncio.Queue[int] = asyncio.Queue()
        self._repair_pending: set[int] = set()
        self._tasks: list[asyncio.Task] = []
        self.started_at = time.time()

    def get(self, account_id: int) -> LiveAccount:
        acc = self.live.get(account_id)
        if acc is None:
            acc = self.live[account_id] = LiveAccount(account_id, pool=self)
        return acc

    async def invalidate(self, account_id: int) -> None:
        """Оператор змінив рядок (проксі/сесію) поза gateway — наступний виклик
        перебудує клієнт. Чекаємо lock, щоб не рвати операцію посередині."""
        acc = self.live.get(account_id)
        if acc is None:
            return
        async with acc.lock:
            await acc.drop()

    def schedule_repair(self, account_id: int) -> None:
        if account_id in self._repair_pending:
            return
        self._repair_pending.add(account_id)
        self._repair_queue.put_nowait(account_id)

    # ---- фонові цикли ----
    def start_background(self) -> None:
        self._tasks = [asyncio.create_task(self._repair_worker(), name="repair"),
                       asyncio.create_task(self._repair_scheduler(), name="repair-sched"),
                       asyncio.create_task(self._idle_worker(), name="idle")]

    async def _repair_worker(self):
        while True:
            account_id = await self._repair_queue.get()
            try:
                await rp.repair(self.get(account_id))
            except Exception as e:  # noqa: BLE001 — ремонт не має валити пул
                logger.exception("repair #%s впав: %r", account_id, e)
            finally:
                self._repair_pending.discard(account_id)

    async def _repair_scheduler(self):
        """Раз на gateway_repair_interval_sec ставить у чергу тих, кому час."""
        from django.db import close_old_connections, connection

        def _tidy():
            # ORM живе в одному executor-потоці; протухле зʼєднання чистимо тут,
            # а не в кожному запиті. Під atomic (тести) — не чіпаємо.
            if not connection.in_atomic_block:
                close_old_connections()
        while True:
            # спершу пауза: одразу після старту нікого не лагодимо, реактивний
            # ремонт (після транспортних збоїв) і так іде через чергу
            interval = await sync_to_async(_setting_int)("gateway_repair_interval_sec", 600)
            await asyncio.sleep(interval)
            await sync_to_async(_tidy)()
            try:
                for account_id in await sync_to_async(rp.due_for_repair)():
                    self.schedule_repair(account_id)
            except Exception as e:  # noqa: BLE001
                logger.exception("repair-scheduler: %r", e)

    async def _idle_worker(self):
        """gateway_idle_disconnect_sec > 0 → відключати клієнтів, що мовчать
        довше. Дефолт 0: тримаємо завжди (акаунт виглядає як живий клієнт)."""
        while True:
            idle = await sync_to_async(_setting_int)("gateway_idle_disconnect_sec", 0)
            if idle > 0:
                now = time.time()
                for acc in list(self.live.values()):
                    if acc.client is not None and not acc.lock.locked() \
                            and acc.last_used and now - acc.last_used > idle:
                        async with acc.lock:
                            await acc.drop()
            await asyncio.sleep(60)

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        # дати операціям завершитись, потім усіх відключити
        for acc in list(self.live.values()):
            try:
                await asyncio.wait_for(acc.lock.acquire(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("shutdown: акаунт #%s не звільнився за 30с — рву", acc.id)
            try:
                await acc.drop()
            finally:
                if acc.lock.locked():
                    acc.lock.release()
        logger.info("shutdown: усіх відключено")

    def stats(self) -> dict:
        rows = [a.stats() for a in self.live.values()]
        return {"accounts_seen": len(rows),
                "connected": sum(1 for r in rows if r["connected"]),
                "locked": sum(1 for r in rows if r["locked"]),
                "ops_ok": sum(r["ops_ok"] for r in rows),
                "ops_failed": sum(r["ops_failed"] for r in rows),
                "repair_pending": len(self._repair_pending),
                "uptime_sec": int(time.time() - self.started_at)}
