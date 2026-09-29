import json
from pathlib import Path

import pytest

from app import parse

F = Path(__file__).parent / "fixtures"


def fx(name):
    return (F / name).read_text(encoding="utf-8")


def test_number_formats():
    assert parse.number("108 566") == 108566
    assert parse.number("3.3k") == 3300
    assert parse.number("-4 107") == -4107
    assert parse.number("+19 623") == 19623
    assert parse.number("6.5%") == 6.5
    assert parse.number("—") is None


def test_channel_search_cards():
    items = parse.parse_peers(json.loads(fx("channels_search.json"))["html"])
    assert len(items) == 30
    first = items[0]
    assert first["handle"] == "arigus" and first["kind"] == "channel"
    assert (first["subscribers"], first["avg_post_reach"], first["ci"]) == (108566, 3300, 284)
    assert first["category"] == "Новости и СМИ"
    assert first["tme_url"] == "https://t.me/arigus"
    assert first["tgstat_stat_url"] == "https://tgstat.ru/channel/@arigus/stat"


def test_catalog_chats_including_private():
    items = parse.parse_peers(json.loads(fx("tag_items.json"))["html"])
    assert all(i["kind"] == "chat" for i in items)
    assert items[0]["handle"] == "antidpsuu03" and items[0]["subscribers"] == 29033
    private = [i for i in items if i["handle"] is None]
    assert private and private[0]["tme_url"] is None
    assert private[0]["tgstat_url"].startswith("https://tgstat.ru/chat/")


def test_channel_stat_page():
    c = parse.parse_channel_stat(fx("channel_stat.html"))
    assert (c["title"], c["handle"], c["category"]) == ("РИА Новости", "rian_ru", "Новости и СМИ")
    assert c["verified"] and c["rkn_registered"]
    s = c["stats"]
    assert s["subscribers"]["value"] == 3039296
    assert s["subscribers"]["details"]["month"] == 19623
    assert s["ci"]["value"] == 20200
    assert s["avg_post_reach"]["details"] == {"err": 7.0, "err24": 6.5}
    assert s["age"]["details"]["created"] == "28.02.2017"
    assert s["posts"]["value"] == 344941


def test_posts_search_page():
    html = fx("posts_search.html")
    posts = parse.parse_posts(html)
    assert parse.found_count(html) == 2 and len(posts) == 2
    p = posts[0]
    assert p["tme_post_url"] == "https://t.me/indirimvarsa/122908"
    assert p["tgstat_post_url"] == "https://tgstat.ru/channel/@indirimvarsa/122908"
    assert p["views"] == 49 and p["forwards"] == 1
    # підсвітка збігу <mark> не рве слово
    assert "alımda" in p["text"]
    assert parse.search_form_state(html) == {"page": "1", "offset": "20"}


def test_nothing_found():
    assert parse.nothing_found(fx("posts_not_found.html"))


def test_captcha_is_detected_not_parsed():
    with pytest.raises(parse.Restricted):
        parse.parse_json(fx("restricted.json"))
