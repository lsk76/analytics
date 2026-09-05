"""
Telethon client wrapper over TelegramAccount — auth (code flow) + enrichment.

Enrichment used by the pipeline:
  * get_message_date(url)   — reliable publish date (UTC)
  * get_channel_meta(handle)— title/description/subscribers for region fallback & cache
"""
import asyncio
import re
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.errors import (AuthKeyUnregisteredError, PhoneNumberBannedError,
                             SessionRevokedError, UserDeactivatedBanError,
                             UserDeactivatedError)
from telethon.sessions import StringSession


def run_async(coro):
    """Run a coroutine from sync Django code, even if a loop already exists."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import threading
            result = {}

            def _runner():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    result["value"] = new_loop.run_until_complete(coro)
                finally:
                    new_loop.close()

            t = threading.Thread(target=_runner)
            t.start()
            t.join()
            return result.get("value")
    except RuntimeError:
        pass
    return asyncio.run(coro)


class TelegramUserClient:
    """Thin wrapper using a TelegramAccount's stored StringSession + proxy."""

    @staticmethod
    def parse_telegram_url(url: str) -> Optional[Tuple[str, int]]:
        """t.me/<handle>/<id> or t.me/c/<internal>/<id> -> (handle_or_cid, msg_id)."""
        m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", url)
        if m:
            return (f"-100{m.group(1)}", int(m.group(2)))
        m = re.match(r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)", url)
        if m:
            return (m.group(1), int(m.group(2)))
        return None

    @staticmethod
    def _prime_proxy(account) -> None:
        """Force-load account.proxy (FK) синхронно, поки ще немає активного event loop.

        Ліниве звернення до FK усередині asyncio.run() (з _client, викликаного
        з корутини) валить Django SynchronousOnlyOperation — ORM забороняє
        неявний доступ з активного loop'а.
        """
        account.proxy  # noqa: B018 — навмисно, лише щоб прогріти кеш FK

    @classmethod
    def _client(cls, account) -> TelegramClient:
        proxy = account.proxy.to_telethon_proxy() if account.proxy else None
        return TelegramClient(
            StringSession(account.session_string or ""),
            int(account.api_id), account.api_hash, proxy=proxy,
            # Відбиток пристрою імпортованої сесії. Без нього Telethon підставить
            # свій дефолт, і в «Активних сесіях» акаунта пристрій СТРИБНЕ — для
            # Telegram це ознака вкраденої сесії. Порожні поля = дефолти Telethon.
            **account.client_kwargs(),
        )

    # ---- перевірка живості (лише читання: get_me, нічого не надсилає) ----
    @classmethod
    def check_alive_sync(cls, account) -> dict:
        """Стан акаунта БЕЗ надсилання повідомлень: connect + get_me.

        Розрізняє живий / розлогінений / забанений / деактивований /
        відкликаний / таймаут проксі. НЕ пише в @SpamBot і нікому іншому.
        """
        async def _run():
            client = cls._client(account)
            try:
                await asyncio.wait_for(client.connect(), timeout=25)
                if not await client.is_user_authorized():
                    return {"state": "розлогінений", "ok": False}
                me = await client.get_me()
                uname = f"@{me.username}" if me.username else "—"
                prem = " · premium" if getattr(me, "premium", False) else ""
                return {"state": "живий", "ok": True, "detail": f"{uname}{prem}"}
            except (UserDeactivatedBanError, PhoneNumberBannedError):
                return {"state": "ЗАБАНЕНИЙ", "ok": False}
            except UserDeactivatedError:
                return {"state": "деактивований", "ok": False}
            except (AuthKeyUnregisteredError, SessionRevokedError):
                return {"state": "сесію відкликано", "ok": False}
            except Exception as e:  # noqa: BLE001
                return {"state": f"помилка: {type(e).__name__}", "ok": False,
                        "detail": str(e)[:80]}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        cls._prime_proxy(account)
        try:
            return run_async(asyncio.wait_for(_run(), timeout=75)) or {"state": "?", "ok": False}
        except Exception:
            return {"state": "таймаут (75с, найімовірніше мертва проксі)", "ok": False}

    # ---- auth (code flow) ----
    @classmethod
    def send_code_sync(cls, account) -> dict:
        async def _run():
            client = cls._client(account)
            await client.connect()
            try:
                sent = await client.send_code_request(account.phone_number)
                return {"success": True, "phone_code_hash": sent.phone_code_hash,
                        "code_type": type(sent.type).__name__,
                        "next_type": type(sent.next_type).__name__ if sent.next_type else None,
                        "session_string": client.session.save()}
            finally:
                await client.disconnect()
        cls._prime_proxy(account)
        res = run_async(_run())
        if res and res.get("success"):
            account.session_string = res["session_string"]
            account.auth_code_hash = res["phone_code_hash"]
            account.save(update_fields=["session_string", "auth_code_hash"])
        return res or {"success": False}

    @classmethod
    def verify_code_sync(cls, account, code: str, password: str = None) -> dict:
        async def _run():
            client = cls._client(account)
            await client.connect()
            try:
                try:
                    await client.sign_in(account.phone_number, code,
                                         phone_code_hash=account.auth_code_hash)
                except Exception as e:
                    if "password" in str(e).lower() and password:
                        await client.sign_in(password=password)
                    else:
                        raise
                return {"success": True, "session_string": client.session.save()}
            finally:
                await client.disconnect()
        cls._prime_proxy(account)
        res = run_async(_run())
        if res and res.get("success"):
            account.session_string = res["session_string"]
            account.is_authenticated = True
            account.auth_code_hash = ""
            account.save(update_fields=["session_string", "is_authenticated", "auth_code_hash"])
        return res or {"success": False}

    # ---- enrichment ----
    @classmethod
    async def _with_client(cls, account, fn):
        client = cls._client(account)
        await client.connect()
        try:
            return await fn(client)
        finally:
            await client.disconnect()

    @classmethod
    async def get_message_date(cls, account, url: str):
        parsed = cls.parse_telegram_url(url)
        if not parsed:
            return None
        handle, msg_id = parsed
        async def fn(client):
            entity = int(handle) if str(handle).lstrip("-").isdigit() else handle
            msg = await client.get_messages(entity, ids=msg_id)
            from datetime import timezone
            return msg.date.astimezone(timezone.utc) if msg and msg.date else None
        return await cls._with_client(account, fn)

    @classmethod
    async def fetch_history(cls, account, handle, min_id: int = 0, limit: int = 50,
                            reverse: bool = False) -> list:
        """Повідомлення каналу після min_id (watermark), до limit штук.

        Полінг історії каналу акаунтом (підписуватись не треба для публічних).
        reverse=False — найновіші перші (для backfill першого полінгу: беремо
        останні N). reverse=True — найстаріші перші ВІД min_id: суцільний догін
        без діри, коли між полінгами накопичилось >limit повідомлень (інакше
        watermark перестрибнув би на найновіший id і пропустив середину).
        Повертає список dict {id, text, date(UTC)}; порожні (медіа) — пропуск."""
        from datetime import timezone as _tz

        async def fn(client):
            entity = int(handle) if str(handle).lstrip("-").isdigit() else handle
            out = []
            async for msg in client.iter_messages(
                    entity, min_id=min_id or 0, limit=limit, reverse=reverse):
                text = (getattr(msg, "message", None) or "").strip()
                if not text:
                    continue
                out.append({
                    "id": msg.id, "text": text,
                    "date": msg.date.astimezone(_tz.utc) if msg.date else None,
                })
            return out
        return await cls._with_client(account, fn)

    # ---- тестовий прогін довільного бота (опитувальники тощо) ----
    @classmethod
    def test_bot_flow_sync(cls, account, bot_username: str, feedback_text: str = "",
                           choices: Optional[list] = None, max_steps: int = 15) -> dict:
        """Пройти /start → серію кнопкових кроків → (за наявності) відгук текстом.

        Не знає наперед текстів/варіантів конкретного бота — фіксує фактичні
        тексти й кнопки кожного кроку, щоб оператор звірив їх вручну.
        choices — індекси кнопок (з 0) для кожного кроку з кнопками; якщо
        коротший за кількість кроків або не заданий — решта кроків тиснуть
        першу кнопку. Крок без кнопок після відправки відгуку вважається
        фінальним.
        """
        async def _wait_reply(client, bot, after_id, after_text, timeout=15.0, interval=0.5):
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                await asyncio.sleep(interval)
                incoming = [m for m in await client.get_messages(bot, limit=5) if not m.out]
                if not incoming:
                    continue
                newest = incoming[0]
                if newest.id != after_id or (newest.text or "") != after_text:
                    return newest
            return None

        async def _run():
            client = cls._client(account)
            steps = []
            await asyncio.wait_for(client.connect(), timeout=25)
            try:
                if not await client.is_user_authorized():
                    return {"ok": False, "error": "акаунт не авторизований", "steps": steps}
                bot = await client.get_entity(bot_username)
                await client.send_message(bot, "/start")
                last_id, last_text, q_index, sent_feedback = 0, "", 0, False
                for i in range(max_steps):
                    msg = await _wait_reply(client, bot, last_id, last_text)
                    if msg is None:
                        steps.append({"step": i + 1, "error": "немає відповіді за 15с"})
                        return {"ok": False, "error": "таймаут очікування відповіді бота",
                                "steps": steps}
                    buttons = [b.text for row in (msg.buttons or []) for b in row]
                    step = {"step": i + 1, "text": msg.text or "", "buttons": buttons}
                    steps.append(step)
                    last_id, last_text = msg.id, msg.text or ""
                    if buttons:
                        idx = choices[q_index] if choices and q_index < len(choices) else 0
                        idx = max(0, min(idx, len(buttons) - 1))
                        step["clicked"] = buttons[idx]
                        q_index += 1
                        await msg.click(idx)
                    elif not sent_feedback and feedback_text:
                        await client.send_message(bot, feedback_text)
                        step["sent_feedback"] = feedback_text
                        sent_feedback = True
                    else:
                        break
                return {"ok": True, "steps": steps}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        cls._prime_proxy(account)
        try:
            return run_async(asyncio.wait_for(_run(), timeout=180)) or {
                "ok": False, "error": "порожній результат", "steps": []}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}", "steps": []}

    @classmethod
    async def get_channel_meta(cls, account, handle: str) -> dict:
        async def fn(client):
            entity = int(handle) if str(handle).lstrip("-").isdigit() else handle
            ent = await client.get_entity(entity)
            full = None
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(ent))
            except Exception:
                pass
            return {
                "tg_id": getattr(ent, "id", None),
                "username": getattr(ent, "username", None),
                "title": getattr(ent, "title", None),
                "description": getattr(full.full_chat, "about", "") if full else "",
                "subscribers": getattr(full.full_chat, "participants_count", 0) if full else 0,
            }
        return await cls._with_client(account, fn)
