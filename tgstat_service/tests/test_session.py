from app.session import (CLOUDFLARE, ERROR, LOGIN_REQUIRED, NO_PREMIUM, OK,
                         classify)

LOGGED = {"url": "https://tgstat.ru/", "title": "TGStat", "csrf": "t",
          "logged_in": True, "user": "Timur", "plan": "Premium"}


def test_premium_session_is_usable():
    s = classify(LOGGED)
    assert (s.state, s.usable, s.user) == (OK, True, "Timur")


def test_cloudflare_wins_over_everything():
    for title in ("Just a moment...", "Attention Required! | Cloudflare"):
        assert classify({**LOGGED, "title": title}).state == CLOUDFLARE


def test_anonymous_page_needs_login():
    s = classify({**LOGGED, "logged_in": False, "user": "", "plan": ""})
    assert (s.state, s.usable) == (LOGIN_REQUIRED, False)


def test_free_plan_is_not_usable():
    assert classify({**LOGGED, "plan": "Free"}).state == NO_PREMIUM


def test_missing_csrf_is_error():
    assert classify({**LOGGED, "csrf": ""}).state == ERROR
