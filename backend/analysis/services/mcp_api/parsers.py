"""Один вердикт по ВСІХ парсингах: що тихо зламалось, поки ніхто не дивився.

«Тихий злам» — це не виняток у логу, а успіх без користі: джерело опитується,
віддає 200, а елементів нуль; TeleZip відповідає, а чанки приходять порожні;
tgstat відкривається, а картка розбирається без підписників. Кожен із цих
сигналів у сервісі вже є, але лежить у різних місцях (`Source.quality_ok`,
`CollectChunk.posts_collected`, `Setting tgstat_selftest_last`), і тому їх ніхто
не дивиться. Тут вони зводяться в один рядок, який можна питати щодня.

Інструмент НЕ ходить у мережу: усе з БД, тож він безкоштовний і його можна
ганяти як завгодно часто. Єдиний живий прогін — `tgstat_selftest` (5 запитів до
tgstat), і сюди потрапляє лише ЙОГО останній результат, а не новий похід.

Окремо стережемо сторожів: детектор, який перестав бігати, гірший за
відсутній — тому вік `last_healthcheck_at` і вік прогону tgstat самі є
перевірками.
"""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from analysis.models import CollectChunk, Post, Setting, Source
from analysis.services.mcp_api import common, fmt
from analysis.services.mcp_api.registry import tool

# --- стелі, за якими «поодинокий мертвий сайт» стає «зламаним парсером» -------
# Одне джерело з 0 елементів — норма життя (сайт помер, редизайн, бан по IP).
# Парсер (чи проксі, чи адаптер) зламався, коли мовчить ЗНАЧНА ЧАСТКА одного
# типу: сайти не домовляються переверстатись одночасно.
BROKEN_SHARE = 0.5
BROKEN_MIN = 3
# Детектор якості ходить раз на годину (worker-info-healthcheck): якщо останній
# прохід старший за це — мовчить не інтернет, а сам воркер.
HEALTHCHECK_STALE_HOURS = 6
# Прогін по живому tgstat робить cron раз на добу (deploy/tgstat-canary.sh).
TGSTAT_STALE_HOURS = 36
# Скільки останніх завершених чанків TeleZip дивимось на «усі порожні».
TELEZIP_LAST_CHUNKS = 20

OK, WARN, BROKEN = "ok", "warn", "broken"
_RANK = {OK: 0, WARN: 1, BROKEN: 2}


def _worst(verdicts) -> str:
    return max(verdicts, key=lambda v: _RANK[v], default=OK)


def _infospace_rows() -> tuple[list, list, list]:
    """Джерела інформпростору по типах: скільки під підозрою якості."""
    rows, problems, verdicts = [], [], []
    src = Source.objects.filter(is_active=True)
    pollable = src.filter(id__in=common.pollable_source_ids())
    by_kind = (pollable.order_by().values("kind")
               .annotate(n=Count("id"),
                         bad=Count("id", filter=Q(quality_ok=False)),
                         failing=Count("id", filter=Q(consecutive_failures__gte=3))))
    for r in sorted(by_kind, key=lambda r: r["kind"]):
        kind, n, bad, failing = r["kind"], r["n"], r["bad"], r["failing"]
        share = bad / n if n else 0
        verdict = OK
        if bad and (bad >= BROKEN_MIN and share >= BROKEN_SHARE):
            verdict = BROKEN
            problems.append(f"{kind}: порожньо розбирається {bad} джерел із {n} "
                            f"({share:.0%}) — це вже не окремі сайти, а адаптер/проксі")
        elif bad:
            verdict = WARN
            problems.append(f"{kind}: {bad} джерел із {n} під підозрою якості")
        verdicts.append(verdict)
        rows.append([kind, n, bad or "—", failing or "—",
                     {OK: "✓", WARN: "🟡", BROKEN: "✗"}[verdict]])
    return rows, problems, verdicts


def _quality_examples(limit: int = 5) -> list:
    """Конкретні джерела з нотаткою — з них людина й починає розбір.

    Той самий фільтр, що й у лічильниках (лише передплачені): інакше таблиця
    показувала б джерела, яких воркер не бере, і суперечила б числам вище.
    """
    bad = (Source.objects.filter(is_active=True, quality_ok=False,
                                 id__in=common.pollable_source_ids())
           .order_by("-last_healthcheck_at")[:limit])
    return [[s.kind, fmt.trunc(s.name, 38), fmt.trunc(s.quality_note, 60),
             fmt.ago(s.last_healthcheck_at)] for s in bad]


def _healthcheck_freshness() -> tuple[str, str, str]:
    """Чи живий сам детектор якості (worker-info-healthcheck)."""
    last = (Source.objects.filter(is_active=True)
            .aggregate(m=Max("last_healthcheck_at"))["m"])
    if not last:
        return BROKEN, "ніколи не бігав", ("worker-info-healthcheck не зробив ні "
                                           "жодного проходу — сигнал якості мертвий")
    age_h = (timezone.now() - last).total_seconds() / 3600
    if age_h > HEALTHCHECK_STALE_HOURS:
        return BROKEN, f"{fmt.ago(last)} тому", (
            f"детектор якості стоїть {fmt.ago(last)} (має раз на годину) — "
            "`service_restart worker-info-healthcheck`")
    return OK, f"{fmt.ago(last)} тому", ""


def _telezip_row() -> tuple[str, str, str]:
    """Чанки збору, що завершились успіхом і нічого не принесли."""
    last = list(CollectChunk.objects.filter(status="done").order_by("-finished_at")
                .values_list("posts_collected", flat=True)[:TELEZIP_LAST_CHUNKS])
    if not last:
        return OK, "завершених чанків немає", ""
    empty = sum(1 for n in last if not n)
    text = f"{len(last) - empty} з {len(last)} останніх чанків принесли пости"
    if empty == len(last):
        return BROKEN, text, ("усі останні чанки TeleZip порожні — найчастіше це "
                              "запит у чужому діалекті (у задачі пробіл = АБО) або "
                              "вікно глибше за індекс; перевірка — tz_status і "
                              "tz_find(stats=true)")
    if empty > len(last) // 2:
        return WARN, text, "більшість останніх чанків TeleZip порожні — звірити запит задачі"
    return OK, text, ""


def _tgstat_row() -> tuple[str, str, str]:
    """Останній ЖИВИЙ прогін розбору tgstat (його робить cron, не цей інструмент)."""
    from analysis.services.mcp_api import tgstat as tgs
    row = Setting.objects.filter(key=tgs.SELFTEST_SETTING).first()
    value = (row.value or "").strip() if row else ""
    if not value:
        return WARN, "ще не запускався", ("живий розбір tgstat ніхто не перевіряв: "
                                          "`tgstat_selftest` або cron "
                                          "deploy/tgstat-canary.sh")
    age = fmt.ago(row.updated_at)
    if " broken:" in value:
        return BROKEN, f"{value} ({age} тому)", ("РОЗБІР tgstat ЗЛАМАВСЯ — "
                                                 "`tgstat_selftest` покаже, що саме; "
                                                 "парсери tgstat_service/app/parse.py")
    stale = (timezone.now() - row.updated_at) > timedelta(hours=TGSTAT_STALE_HOURS)
    if stale:
        return WARN, f"{value} ({age} тому)", (
            f"прогін tgstat старший за {TGSTAT_STALE_HOURS} год — cron стоїть "
            "або сесія весь час непридатна")
    if " unverified:" in value or " unverified" in value:
        return WARN, f"{value} ({age} тому)", ("tgstat не перевірявся: сесія "
                                               "потребує людини (капча/вхід у VNC)")
    return OK, f"{value} ({age} тому)", ""


def _collect_silence() -> tuple[str, str, str]:
    """Збір, що тихо спинився: постів за добу нуль, хоч джерела активні."""
    day_ago = timezone.now() - timedelta(hours=24)
    fresh = common.scope_by_task(Post.objects.filter(created_at__gte=day_ago)).count()
    if fresh:
        return OK, f"{fresh} постів за добу", ""
    active = Source.objects.filter(is_active=True).count()
    if not active:
        return OK, "активних джерел немає", ""
    return BROKEN, "0 постів за добу", ("за добу не зібрано НІ ОДНОГО поста, хоч "
                                        f"активних джерел {active} — дивитись черги "
                                        "(`service_queues`) і воркери збору")


@tool("parsers_health", group="parsers", params={
      "examples": "true — додати конкретні джерела з нотаткою якості"})
def parsers_health(examples: bool = True):
    """Що з парсингів тихо зламалось: джерела, TeleZip, tgstat — одним вердиктом.

    «Тихий злам» — це успіх без користі: сторінка відкривається, а елементів
    нуль. Такі сигнали в сервісі вже збираються (детектор якості джерел раз на
    годину, живий прогін розбору tgstat раз на добу); тут вони зводяться разом,
    щоб одного виклику хватало на щоденну перевірку.

    У мережу НЕ ходить і грошей не витрачає — усе з БД, тож викликати можна
    скільки завгодно. Вердикт: ok — усе розбирається; warn — поодинокі джерела
    або застарілий прогін; broken — зламався адаптер, збір стоїть або розбір
    tgstat поїхав (у кожному рядку сказано, що робити).
    """
    rows, problems, verdicts = _infospace_rows()
    singles = [("детектор якості джерел",) + _healthcheck_freshness(),
               ("збір за добу",) + _collect_silence(),
               ("TeleZip (останні чанки)",) + _telezip_row(),
               ("tgstat (живий розбір)",) + _tgstat_row()]
    for name, verdict, text, hint in singles:
        verdicts.append(verdict)
        if hint:
            problems.append(f"{name}: {hint}")

    verdict = _worst(verdicts)
    head = fmt.kv([
        ("вердикт", {OK: "✓ парсинги працюють", WARN: "🟡 є підозри",
                     BROKEN: "✗ ЩОСЬ ЗЛАМАЛОСЬ"}[verdict]),
        ("перевірено", "по БД, без запитів у мережу"),
    ])
    parts = [head, fmt.section(
        "Джерела інформпростору",
        fmt.table(["тип", "опитується", "порожній розбір", "збоїв ≥3", ""], rows)
        if rows else "активних джерел у полінгу немає")]
    if examples:
        ex = _quality_examples()
        if ex:
            parts.append(fmt.section("Під підозрою (останні)", fmt.table(
                ["тип", "джерело", "чому", "перевірено"], ex)))
    parts.append(fmt.section("Решта підсистем", fmt.table(
        ["що", "стан", ""],
        [[name, text, {OK: "✓", WARN: "🟡", BROKEN: "✗"}[v]]
         for name, v, text, _ in singles])))
    if problems:
        parts.append(fmt.section("Що робити", "\n".join(f"— {p}" for p in problems)))
    return fmt.joinsec(*parts)
