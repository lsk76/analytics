"""Стрім tgsearch: чат читається за хешем, який ЦЕЙ акаунт уже здобув; резолв —
лише при першому контакті акаунта з чатом."""
import pytest

from analysis.models import Channel
from analysis.services import peers
from analysis.services import tgsearch_stages as tgs

pytestmark = pytest.mark.django_db


def test_entity_spec_prefers_per_account_hash():
    ch = Channel.objects.create(username="pub", title="p", tg_id=100,
                                raw_meta={"access_hash_by_acc": {"7": 999}})
    assert tgs._entity_spec(ch, 7) == {"channel_id": 100, "access_hash": 999}
    assert tgs._entity_spec(ch, 8) == {"username": "pub"}          # чужий хеш не береться
    assert tgs._entity_spec(ch) == {"username": "pub"}
    linked = Channel.objects.create(username="linked:parent", title="l")
    assert tgs._entity_spec(linked, 7) == {"linked_parent": "parent"}
    linked.tg_id, linked.raw_meta = 200, {"access_hash": 5}
    assert tgs._entity_spec(linked, 7) == {"channel_id": 200, "access_hash": 5}


def test_apply_resolved_stores_under_account():
    ch = Channel.objects.create(username="pub", title="p")
    tgs._apply_resolved(ch, {"id": 100, "access_hash": 999}, 7)
    ch.refresh_from_db()
    assert ch.tg_id == 100 and ch.raw_meta["access_hash_by_acc"] == {"7": 999}
    assert peers.remember_peer(ch, 7, {"id": 100, "access_hash": 999}) is False   # без змін
    other = Channel.objects.create(username="dup", title="d")
    tgs._apply_resolved(other, {"id": 100, "access_hash": 1}, 8)      # tg_id зайнятий
    other.refresh_from_db()
    assert other.tg_id is None and other.raw_meta["access_hash_by_acc"] == {"8": 1}
