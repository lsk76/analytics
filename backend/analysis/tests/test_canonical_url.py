"""canonical_url: та сама стаття з RSS і зі скрапінгу → один Post."""
from analysis.services.infospace.utils import canonical_url


def test_strips_utm_params():
    # реальний приклад з інвентарю джерел (§14 доки)
    url = ("https://gazetarb.ru/news/v-buryatii-zaderzhali-rukovoditelya/"
           "?utm_source=yxnews&utm_medium=desktop"
           "&utm_referrer=https%3A%2F%2Fdzen.ru%2Fnews%2Fstory%2Fba5605d1")
    assert canonical_url(url) == (
        "https://gazetarb.ru/news/v-buryatii-zaderzhali-rukovoditelya/")


def test_strips_fragment_and_lowercases_host():
    assert canonical_url("https://Baikal-Daily.RU/news/16/518990/#comments") == (
        "https://baikal-daily.ru/news/16/518990/")


def test_keeps_non_tracking_params_drops_click_ids():
    url = "https://dzen.ru/news/story/abc?lang=ru&page=1&yclid=555&fbclid=x"
    assert canonical_url(url) == "https://dzen.ru/news/story/abc?lang=ru&page=1"


def test_param_order_preserved():
    url = "https://a.example/x?b=2&a=1&utm_source=s"
    assert canonical_url(url) == "https://a.example/x?b=2&a=1"


def test_passthrough_non_urls():
    # telegram-ідентифікатори лишаються як є
    assert canonical_url("@ulan_smi") == "@ulan_smi"
    assert canonical_url("") == ""


def test_path_case_and_trailing_slash_untouched():
    # регістр шляху значущий на багатьох сайтах — НЕ нормалізуємо
    assert canonical_url("https://site.example/News/A/") == (
        "https://site.example/News/A/")


def test_idempotent():
    once = canonical_url("https://a.example/c?utm_x=1&d=2#frag")
    assert canonical_url(once) == once


def test_malformed_url_passes_through_not_raises():
    # биті href зі скрапінгу не мають валити стадію збору (urlsplit кидає
    # ValueError на «Invalid IPv6 URL») — повертаємо як є
    bad = "http://[dead-link/x"
    assert canonical_url(bad) == bad


def test_port_in_host_preserved():
    assert canonical_url("https://Site.example:8443/A?utm_x=1") == (
        "https://site.example:8443/A")
