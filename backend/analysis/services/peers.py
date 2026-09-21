"""Кеш (channel_id, access_hash) чату ПІД КОНКРЕТНИЙ АКАУНТ.

Резолв юзернейма (ResolveUsernameRequest) потрібен один раз на пару
«акаунт + чат»: далі читати можна за (id, access_hash) без добового ліміту.
Хеш персональний — здобутий одним акаунтом для іншого недійсний
(ChannelInvalidError), тому мапа `Channel.raw_meta["access_hash_by_acc"]`
= {id акаунта: хеш}. Той самий формат використовує публікація (_peer_of/_save_peer).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEY = "access_hash_by_acc"


def peer_for(channel, account_id: int) -> dict | None:
    """{"channel_id", "access_hash"} для gateway-spec, якщо цей акаунт уже
    резолвив чат; інакше None (споживач передасть юзернейм)."""
    if channel is None or not account_id or not channel.tg_id:
        return None
    ah = ((channel.raw_meta or {}).get(KEY) or {}).get(str(account_id))
    return {"channel_id": int(channel.tg_id), "access_hash": int(ah)} if ah else None


def remember_peer(channel, account_id: int, peer: dict | None) -> bool:
    """Зберегти хеш, який gateway щойно повернув у `resolved`. tg_id пишемо
    лише якщо вільний: той самий канал буває в довіднику двічі під різними
    юзернеймами, і запис ламав uniq_channel_tgid."""
    if channel is None or not account_id or not peer or not peer.get("access_hash"):
        return False
    from django.db import IntegrityError, transaction

    Model = type(channel)
    meta = dict(channel.raw_meta or {})
    by_acc = dict(meta.get(KEY) or {})
    key = str(account_id)
    if by_acc.get(key) == peer["access_hash"] and channel.tg_id:
        return False
    by_acc[key] = int(peer["access_hash"])
    meta[KEY] = by_acc
    fields = {"raw_meta": meta}
    if not channel.tg_id and peer.get("id") and not (
            Model.objects.filter(tg_id=peer["id"]).exclude(pk=channel.pk).exists()):
        fields["tg_id"] = int(peer["id"])
    try:
        with transaction.atomic():
            Model.objects.filter(pk=channel.pk).update(**fields)
    except IntegrityError:
        logger.debug("remember_peer: %s — конфлікт унікальності, пропускаю", channel.pk)
        return False
    channel.raw_meta = meta
    if "tg_id" in fields:
        channel.tg_id = fields["tg_id"]
    return True


def forget_peer(channel, account_id: int) -> bool:
    """Кешований хеш виявився недійсним (ChannelInvalidError): забути його під
    цей акаунт, наступне читання піде за юзернеймом і перезбере хеш."""
    if channel is None or not account_id:
        return False
    meta = dict(channel.raw_meta or {})
    by_acc = dict(meta.get(KEY) or {})
    if str(account_id) not in by_acc:
        return False
    by_acc.pop(str(account_id))
    meta[KEY] = by_acc
    type(channel).objects.filter(pk=channel.pk).update(raw_meta=meta)
    channel.raw_meta = meta
    return True


def is_stale_peer_error(err: str) -> bool:
    return "ChannelInvalid" in (err or "") or "CHANNEL_INVALID" in (err or "")
