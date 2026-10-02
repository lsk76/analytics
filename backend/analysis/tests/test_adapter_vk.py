"""VK-адаптер інформпростору: watermark, закріплений пост, повторний полінг, темп."""
from unittest import mock

import pytest

from analysis.services import vk
from analysis.services.infospace.adapters import get_adapter
from analysis.services.infospace.adapters.base import RateLimited
from analysis.services.infospace.adapters.vk import VkAdapter


class _Src:
    """Мінімальний дубль Source (адаптер у БД не пише)."""
    def __init__(self, poll_cursor=None, config=None):
        self.url = "https://vk.com/club42"
        self.name = "Спільнота"
        self.poll_cursor = poll_cursor or {}
        self.config = config or {}


def _wall(items):
    return mock.patch.object(vk, "wall_get", return_value={"items": items})


def _post(pid, text="текст", pinned=False, ts=1759000000):
    out = {"id": pid, "date": ts, "text": text}
    if pinned:
        out["is_pinned"] = 1
    return out


def test_registered():
    assert isinstance(get_adapter("vk"), VkAdapter)


def test_first_poll_sets_watermark_and_resolves_owner_once():
    src = _Src()
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")) as res, \
            _wall([_post(7), _post(6)]):
        items = VkAdapter().fetch(src)
    assert [i.external_id for i in items] == ["7", "6"]
    assert items[0].url == "https://vk.com/wall-42_7"
    assert src.poll_cursor == {"owner_id": -42, "last_post_id": 7}
    assert res.call_count == 1

    # другий полінг бере owner_id із курсора і нічого нового не віддає
    with mock.patch.object(vk, "resolve_owner", side_effect=AssertionError("зайвий резолв")), \
            _wall([_post(7), _post(6)]):
        assert VkAdapter().fetch(src) == []


def test_pinned_post_is_not_a_watermark():
    """Закріплений приходить першим і може бути дуже старим — межею бути не може."""
    src = _Src(poll_cursor={"owner_id": -42, "last_post_id": 5})
    with _wall([_post(1, pinned=True), _post(6)]):
        items = VkAdapter().fetch(src)
    assert [i.external_id for i in items] == ["6"]
    assert src.poll_cursor["last_post_id"] == 6


def test_repost_body_and_empty_posts():
    src = _Src(poll_cursor={"owner_id": -42, "last_post_id": 0})
    with _wall([{"id": 9, "date": 1759000000, "text": "",
                 "copy_history": [{"text": "тіло репоста"}]},
                _post(8, text="")]):
        items = VkAdapter().fetch(src)
    assert [i.text for i in items] == ["тіло репоста"]      # фото без підпису пропущено
    assert items[0].meta["vk"]["is_repost"] is True
    assert src.poll_cursor["last_post_id"] == 9             # але watermark по обох


def test_rate_limit_becomes_adapter_pause():
    src = _Src(poll_cursor={"owner_id": -42, "last_post_id": 0})
    with mock.patch.object(vk, "wall_get",
                           side_effect=vk.VkRateLimited("часто", retry_after=7)):
        with pytest.raises(RateLimited) as e:
            VkAdapter().fetch(src)
    assert e.value.retry_after == 7


def test_backfill_limit_on_first_poll():
    src = _Src(config={"backfill_limit": 2})
    with mock.patch.object(vk, "resolve_owner", return_value=(-42, "group")), \
            _wall([_post(9), _post(8), _post(7)]):
        items = VkAdapter().fetch(src)
    assert len(items) == 2
