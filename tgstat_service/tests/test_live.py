"""ЖИВІ перевірки розбору tgstat — єдині тести тут, що ходять у справжній сайт.

Решта тестів працює на заготовках і стереже НАШ код; ці — ловлять день, коли
tgstat перевіршує розмітку, і наші парсери почнуть тихо віддавати порожнє.

Вимикач — змінна середовища: без неї тести пропускаються, бо КОЖЕН прогін це 5
запитів до tgstat, а він на частоту відповідає капчею і тоді відмовляє всім.

    TGSTAT_LIVE=1 TGSTAT_API=http://tgstat:8020 python -m pytest tests/test_live.py -v

Періодично це робить не pytest, а cron на проді (`deploy/tgstat-canary.sh`, раз
на добу) — у нього й дивитись, якщо цікаво «коли зламалось».
"""
import json
import os
import urllib.error
import urllib.request

import pytest

API = os.environ.get("TGSTAT_API", "http://tgstat:8020")

pytestmark = pytest.mark.skipif(
    os.environ.get("TGSTAT_LIVE", "") not in ("1", "true", "yes"),
    reason="живий прогін коштує 5 запитів до tgstat: TGSTAT_LIVE=1, щоб увімкнути")


def _get(path: str, timeout: int = 600):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except urllib.error.URLError as e:
        pytest.fail(f"сервіс tgstat не відповідає на {API}: {e}")


@pytest.fixture(scope="module")
def report():
    """Один прогін на весь модуль: п'ять запитів, не більше."""
    status, body = _get("/selftest")
    if body.get("verdict") == "unverified":
        pytest.skip(f"сесія tgstat непридатна ({body.get('state')}): "
                    f"{body.get('detail')} — спершу вхід у VNC, це не поломка розбору")
    assert status == 200, body
    return body


def test_markup_still_parses(report):
    """Головне твердження: розбір живого tgstat дає дані, а не порожнє."""
    broken = [c for c in report["checks"] if not c["ok"]]
    assert not broken, "РОЗМІТКА TGSTAT ЗМІНИЛАСЬ — " + "; ".join(
        f"{c['name']}: {'; '.join(c.get('problems') or [])} (сире: {c.get('raw')})"
        for c in broken)


def test_every_check_ran(report):
    """Перевірка, що тихо зникла зі звіту, — теж поломка."""
    from app import selftest
    assert [c["name"] for c in report["checks"]] == [n for n, _ in selftest.CHECKS]
    assert report["requests"] == len(selftest.CHECKS)


def test_one_run_is_cheap_enough_to_schedule(report):
    """Запобіжник проти «додам ще перевірку»: прогін мусить лишатись дешевим."""
    assert report["requests"] <= 6, ("живий прогін подорожчав — на щоденному cron "
                                     "це пряма дорога до капчі")
