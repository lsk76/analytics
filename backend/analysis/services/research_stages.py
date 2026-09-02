"""
Конвеєр «Тематичне дослідження» (pipeline="research") — формалізація разових
досліджень задачі 6 (етнічна напруга C2-C4, економіка E1-E4, політика P1-P3).

Історична схема (docs/ethnic-events-pipeline.md, docs/econ-events-pipeline.md):
  локальні канали республік ('*', unique, по одному каналу) → keyword-AND-фільтр
  за рубриками → агентна класифікація (гео=республіка + справжня подія) →
  групування дублів у інциденти → Event з тегом рубрики.

Тут те саме, але постійним конвеєром:
  res_collect  = mon_collect (той самий код: whitelist MonitorChat задачі,
                 '*' unique по каналу; стадії постів переюзаємо mon_*)
  res_filter   : mon_collected -> mon_prescreened (кандидат: довжина >=40 і
                 БУДЬ-ЯКА активна рубрика спрацювала; ключі рубрик у
                 classification._rubrics) | done (не кандидат)
  res_runs     : ранер запусків — готує пачки кандидатів для агентів
                 («чекає агента»), після *_done.json групує підтверджені у
                 події (рубрика+регіон+дата±3д+схожість підсумку) і фінішує.

Граблі, перенесені з доків: гео-правило в промпті обов'язкове; C4-пастка
держпропаганди; AND-фільтр (OR дає шум ×30); URL TeleZip ненадійний — аудит
лише по тексту.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter

from django.db import transaction
from django.utils import timezone as djtz
from rapidfuzz import fuzz

from analysis.models import (CollectChunk, Event, Post, Region, ResearchRun,
                             Tag, TagCategory, _default_research_audit_prompt)

from .monitor_stages import _claim  # клейм-механіка постів (як у monitor)
from . import pipeline_runs as PR

log = logging.getLogger(__name__)

# --- ОЦІНКА ЦЕНТРУ як тег ---------------------------------------------------
# Метрика замовника — ЧАСТКА критичних згадок серед УСІХ згадок федвлади,
# привʼязаних до суб'єкта. Тому питання рівно два: (1) чи є згадка федвлади
# щодо цього суб'єкта — це знаменник; (2) як текст характеризує ЦЕНТР — це
# чисельник. Вісь живе і в classification поста (для скриптових підрахунків),
# і ТЕГОМ — фасети адмінки й графіки читають лише Tag, не JSON-поле.
#
# ЧОМУ САМЕ «ОЦІНКА ЦЕНТРУ», А НЕ «ТОН НОВИНИ» (граблі, виміряно 2026-08-14 на
# зрізі за 3 серпня): попередня схема питала тон новини, і дві третини «негативу»
# виявились негативом на адресу РЕГІОНАЛЬНОЇ влади, поки федеральний орган її
# карав — прокуратура судиться з мером. Образ центру це не псує, а в метрику
# потрапляло. Тепер оцінюється тільки центр, адресат зашитий у саме питання.
STANCE_CATEGORY = "fed_stance"
STANCE_LABEL = "Оцінка центру"
STANCE_TAGS = {"критично": "оцінка_критично",
               "нейтрально": "оцінка_нейтрально",
               "схвально": "оцінка_схвально"}


# --- ТИП ЗГАДКИ (описова вісь) ----------------------------------------------
# НЕ фільтр і НЕ множник: на метрику не впливає, знаменник не ріже. Пояснює, з
# чого складається присутність центру — з власних рішень чи з програмної вивіски
# над регіональною роботою («відремонтували за нацпроєктом»). У вибірці 60 постів
# друге дало 45%. Нацпроєкт — теж дія центру, тому лишається в знаменнику.
MENTION_CATEGORY = "fed_mention"
MENTION_LABEL = "Тип згадки центру"
MENTION_TAGS = {"рішення": "згадка_рішення",
                "програма": "згадка_програма"}


def _axis_tag(key: str, label: str, mapping: dict, value: str, order: int):
    """Tag осі; None для невідомого значення (не засмічуємо закритий реєстр).
    ІНВАРІАНТ: рядок TagCategory має існувати, інакше фасет-фільтр адмінки не
    реєструється і будь-який URL з tag_<key> падає на ?e=1."""
    name = mapping.get((value or "").strip().lower())
    if not name:
        return None
    TagCategory.objects.get_or_create(
        key=key, defaults=dict(label=label, closed=True, order=order))
    tag, _ = Tag.objects.get_or_create(name=name, category=key)
    return tag


def stance_tag(stance: str):
    return _axis_tag(STANCE_CATEGORY, STANCE_LABEL, STANCE_TAGS, stance, 45)


def mention_tag(kind: str):
    return _axis_tag(MENTION_CATEGORY, MENTION_LABEL, MENTION_TAGS, kind, 46)


RES_MIN_LEN = 40          # історичний фільтр довжини (collect_econ_multi.py)
BATCH_SIZE = 50           # історичний розмір пачки класифікації
GROUP_DAYS = 3            # вікно склейки дублів одного інциденту
GROUP_FUZZ = 70           # поріг схожості підсумків (token_set_ratio)


# --------------------------------------------------------------------------- фільтр за рубриками

def _compile_rubrics(task):
    """[(key, [regex, regex, ...])] — ІСТОРИЧНА семантика (ТЕМА ∧ ДІЯ):
    keywords рубрики = список РЕГУЛЯРОК; кандидат, якщо КОЖНА збіглась
    (регістронезалежно). Бита регулярка — рубрика пропускається з логом."""
    out = []
    for r in task.rubrics.filter(is_active=True).order_by("order", "key"):
        pats = []
        try:
            for pat in (r.keywords or []):
                pat = str(pat).strip()
                if pat:
                    pats.append(re.compile(pat, re.I))
        except re.error as e:
            log.error("res_filter: рубрика %s — бита регулярка: %s", r.key, e)
            continue
        if pats:
            out.append((r.key, pats))
    return out


def _match_rubrics(text: str, compiled) -> list[str]:
    return [key for key, pats in compiled
            if all(p.search(text) for p in pats)]


def res_filter_once(task):
    """Кандидати за рубриками: kept -> mon_prescreened (+_rubrics), шум -> done."""
    ids = _claim(task, Post.STAGE_MON_COLLECTED, 1000)
    if not ids:
        return False
    compiled = _compile_rubrics(task)
    posts = list(Post.objects.filter(id__in=ids))
    n_kept = n_drop = 0
    for p in posts:
        text = (p.text or "").strip()
        keys = _match_rubrics(text, compiled) if len(text) >= RES_MIN_LEN else []
        cl = dict(p.classification or {})
        if keys:
            cl["_rubrics"] = keys
            p.stage = Post.STAGE_MON_PRESCREENED
            n_kept += 1
        else:
            cl["is_filtered"] = True
            cl["exclusion_label"] = "no_rubric"
            p.is_relevant = False
            p.stage = Post.STAGE_DONE
            n_drop += 1
        p.classification = cl
        p.stage_locked_at = None
    Post.objects.bulk_update(posts, ["classification", "stage", "stage_locked_at",
                                     "is_relevant"], batch_size=500)
    log.info("res_filter[%s]: kept=%s drop=%s", task.slug, n_kept, n_drop)
    return True


# --------------------------------------------------------------------------- ранер запусків

def res_runs_once(task) -> bool:
    """ЗАМОК НА ЗАПУСК ОБОВʼЯЗКОВИЙ. Стадії тут не ідемпотентні всередині себе:
    _create_events спершу зносить попередні події вікна, потім створює нові. Два
    процеси (фоновий воркер + ручний `run_worker --once`) заходять одночасно,
    кожен бачить порожньо-очищене вікно і створює СВІЙ комплект подій; пости
    дістаються тому, хто записав останнім, а перший комплект лишається сиротами
    без постів. Виміряно 2026-08-15 на run #34: 346 живих подій + 343 порожніх.
    select_for_update(skip_locked) → другий процес просто пропускає цей запуск."""
    did = False
    ids = list(ResearchRun.objects
               .filter(task=task, status__in=["collecting", "collected", "awaiting_agent"])
               .order_by("id").values_list("id", flat=True))
    for run_id in ids:
        try:
            with transaction.atomic():
                run = (ResearchRun.objects.select_for_update(skip_locked=True)
                       .filter(id=run_id).first())
                if run is None:
                    continue          # інший процес уже веде цей запуск
                if run.status == "awaiting_agent":
                    did = _try_ingest(run) or did
                elif run.status in ("collecting", "collected"):
                    did = _try_prepare(run) or did
        except Exception as e:  # noqa: BLE001
            ResearchRun.objects.filter(id=run_id).update(error=repr(e)[:2000])
            log.exception("res_runs: run #%s advance failed", run_id)
    return did


def _window_posts(run):
    return Post.objects.filter(task=run.task,
                               posted_at__date__gte=run.date_from,
                               posted_at__date__lte=run.date_to)


def _try_prepare(run) -> bool:
    if CollectChunk.objects.filter(job=run).exclude(status__in=["done", "split"]).exists():
        return False
    if _window_posts(run).filter(stage=Post.STAGE_MON_COLLECTED).exists():
        return False   # фільтр ще працює
    cands = list(_window_posts(run)
                 .filter(stage=Post.STAGE_MON_PRESCREENED, is_classified=False)
                 .select_related("channel", "region_subject")
                 .order_by("posted_at", "id"))
    bdir = PR.batch_dir(run)
    os.makedirs(bdir, exist_ok=True)
    with open(f"{bdir}/SYSTEM_PROMPT.md", "w") as f:
        f.write(_agent_prompt(run.task))
    n_b = 0
    for i in range(0, len(cands), BATCH_SIZE):
        n_b += 1
        items = [{"id": p.id,
                  "chat": p.channel_name,
                  "region": p.region_subject.name if p.region_subject_id else "",
                  "rubrics_hint": (p.classification or {}).get("_rubrics", []),
                  "date": p.posted_at.date().isoformat() if p.posted_at else None,
                  "text": (p.text or "")[:1500]}
                 for p in cands[i:i + BATCH_SIZE]]
        with open(f"{bdir}/batch_{n_b:03d}.json", "w") as f:
            json.dump({"meta": {"task_slug": run.task.slug, "batch_id": n_b,
                                "kind": "research"},
                       "items": items}, f, ensure_ascii=False)
    stats = dict(run.stats or {})
    stats.update(batch_dir=bdir, batches=n_b, batches_done=0, kind="research",
                 candidates=len(cands))
    if not n_b:
        _finish(run, stats)
        return True
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("res_runs: run #%s → awaiting_agent (%s кандидатів, %s пачок)",
             run.id, len(cands), n_b)
    return True


def _agent_prompt(task) -> str:
    """Промпт агента: база з задачі (tagger_prompt) + правила рубрик."""
    parts = [task.tagger_prompt or ""]
    parts.append("\nRUBRICS (use these exact keys in the verdict):")
    for r in task.rubrics.filter(is_active=True).order_by("order", "key"):
        parts.append(f"- {r.key} — {r.name}")
        if r.extra_prompt:
            parts.append(f"  {r.extra_prompt.strip()}")
    return "\n".join(parts)


CLUSTER_PROMPT = """You merge duplicate incidents. Below is a list of incident summaries from
LOCAL Russian-republic channels (each already a mechanical pre-group of near-
identical reposts). Some describe the SAME real-world event, reported by
different channels with different wording. Return which incident ids describe
the SAME event.

MERGE only if it is clearly the SAME concrete event (same place + same actors +
same action + close in time). Different corruption cases, different officials,
different protests — DO NOT merge, even if same rubric/region. When in doubt,
keep separate.

Respond with STRICT JSON only:
{"same_event_groups": [[id, id, ...], ...]}
List ONLY groups of 2+ ids that must be merged; singletons are implicit.
"""


def _try_ingest(run) -> bool:
    """Три фази інжесту: (1) classify — зібрати вердикти + МЕХАНІЧНЕ пре-групування
    (дешево склеює очевидні репости), готує LLM-крок кластеризації; (2) cluster —
    агент зливає інциденти «одна подія різними словами» (як історичний скрипт);
    (3) audit — ОПЦІЙНИЙ агент-аудит створених подій (keep/reject), симетрично
    events-конвеєру (pipeline_runs._ev_try_ingest). Механіка перша → LLM бачить
    лише ~десятки інцидентів, а не сотні постів."""
    bdir = PR.batch_dir(run)
    stats = dict(run.stats or {})
    phase = stats.get("phase", "classify")
    if phase == "audit":
        return _ingest_audit(run, bdir, stats)
    if phase == "cluster":
        return _ingest_cluster(run, bdir, stats)
    return _ingest_classify(run, bdir, stats)


def _ingest_classify(run, bdir, stats) -> bool:
    todo, done = PR._batches(bdir)
    if stats.get("batches_done") != len(done):
        stats["batches_done"] = len(done)
        run.stats = stats
        run.save(update_fields=["stats"])
    if len(done) < len(todo):
        return False

    # 1) зібрати вердикти
    confirmed = []   # (post_id, rubrics, summary, region_name, stance, mention)
    post_ids = set()
    for f in sorted(done):
        data = json.load(open(f))
        for it in (data.get("items") or []):
            pid = it.get("id")
            v = it.get("verdict") or {}
            rubs = [k for k in (v.get("rubrics") or []) if k]
            if pid:
                post_ids.add(pid)
            if rubs:
                confirmed.append((pid, rubs, (v.get("summary") or "")[:500],
                                  (v.get("region") or "").strip(),
                                  (v.get("stance") or "").strip(),
                                  (v.get("mention_kind") or "").strip()))

    posts = {p.id: p for p in
             Post.objects.filter(id__in=[c[0] for c in confirmed])
             .select_related("channel", "region_subject")}

    # ГЕО З ВЕРДИКТУ, А НЕ З КАНАЛУ. Post.region_subject денормалізується з
    # Channel, тож канал, який пише про сусідній суб'єкт, давав хибну прив'язку
    # (виміряно на пробному запуску: 6 подій із 26 — Якутія в «Бурятії»,
    # Новосибірськ в «Іркутській»). Агент бачить текст і визначає регіон вірно;
    # беремо його, а канальний лишаємо лише як запасний.
    region_by_name = {r.name.strip().lower(): r.id
                      for r in Region.objects.all()}
    # ОЦІНКА — НА ПОСТІ. Знаменник метрики це ПУБЛІКАЦІЇ, а не події: події
    # склеюють дублі й занизили б активно обговорювані теми. Пишемо і в
    # classification (для скриптових підрахунків), і ТЕГОМ (щоб працювали
    # фасет-фільтр і графіки адмінки).
    stale = list(Tag.objects.filter(
        category__in=[STANCE_CATEGORY, MENTION_CATEGORY,
                      # осі попередньої схеми: якщо пост перетеговують, старі
                      # значення мусять зникнути, інакше зріз змішає дві схеми
                      "fed_tone", "fed_neg_target"]))
    # ТЕГ РУБРИКИ — ТЕЖ НА ПОСТІ. Раніше він жив лише на події, і зріз «пости
    # з K2» був порожній, хоча метрика рахується САМЕ по постах: події склеюють
    # дублі. Крім того частина постів лишається без події (аудит відхилив), а зі
    # знаменника вони випадати не повинні — згадка в каналі однаково була.
    rubric_tag = {}
    rub_cats = set()
    for r in run.task.rubrics.all():
        if r.tag_category and r.tag_name:
            rub_cats.add(r.tag_category)
            rubric_tag[r.key], _ = Tag.objects.get_or_create(
                category=r.tag_category, name=r.tag_name)
    stale += list(Tag.objects.filter(category__in=rub_cats))
    n_fixed = n_stance = 0
    for pid, rubs, _s, rname, stance, mkind in confirmed:
        p = posts.get(pid)
        if not p:
            continue
        upd = []
        rid = region_by_name.get(rname.lower()) if rname else None
        if rid and rid != p.region_subject_id:
            p.region_subject_id = rid
            upd.append("region_subject")
            n_fixed += 1
        cl = dict(p.classification or {})
        cl["rubrics"] = rubs        # ВЕРДИКТ агента (≠ механічний хінт _rubrics)
        cl["stance"] = stance or None
        cl["mention_kind"] = mkind or None
        cl.pop("tone", None)        # рудименти попередньої схеми
        cl.pop("neg_target", None)
        p.classification = cl
        upd.append("classification")
        n_stance += int(bool(stance))
        p.save(update_fields=upd)
        if stale:
            p.tags.remove(*stale)
        p.tags.add(*[t for k in rubs if (t := rubric_tag.get(k))])
        if stance and (tag := stance_tag(stance)):
            p.tags.add(tag)
        if mkind and (mt := mention_tag(mkind)):
            p.tags.add(mt)
    if n_fixed or n_stance:
        log.info("res_runs: run #%s — гео виправлено у %s постах, оцінку записано у %s",
                 run.id, n_fixed, n_stance)

    # 2) МЕХАНІЧНЕ пре-групування: рубрика+регіон, дата±вікно, схожий підсумок
    #    (пороги — з полів задачі, дефолти GROUP_DAYS/GROUP_FUZZ)
    g_days = run.task.dedup_group_days or GROUP_DAYS
    g_fuzz = run.task.dedup_group_fuzz or GROUP_FUZZ
    groups = []   # {rubric, region_id, date, summary, member_ids:[...]}
    for pid, rubs, summary, _rname, _stance, _mk in sorted(
            confirmed, key=lambda c: (posts[c[0]].posted_at if c[0] in posts and posts[c[0]].posted_at else djtz.now())):
        p = posts.get(pid)
        if not p:
            continue
        d = p.posted_at.date() if p.posted_at else None
        rub = rubs[0]
        placed = False
        for g in groups:
            if g["rubric"] != rub or g["region_id"] != p.region_subject_id:
                continue
            if d and g["_d"] and abs((d - g["_d"]).days) > g_days:
                continue
            if fuzz.token_set_ratio(summary, g["summary"]) >= g_fuzz:
                g["member_ids"].append(pid)
                placed = True
                break
        if not placed:
            groups.append({"rubric": rub, "region_id": p.region_subject_id,
                           "_d": d, "date": d.isoformat() if d else None,
                           "summary": summary, "member_ids": [pid]})

    # зберегти пре-групи + список постів для фази cluster
    for g in groups:
        g.pop("_d", None)   # date-обʼєкт не серіалізується — лишаємо isoformat
    with open(f"{bdir}/_incidents.json", "w") as f:
        json.dump({"groups": groups, "post_ids": list(post_ids),
                   "confirmed_ids": [c[0] for c in confirmed]}, f, ensure_ascii=False)

    # 3) LLM-крок зайвий, якщо інцидентів ≤1 у кожній парі рубрика+регіон
    rr = Counter((g["rubric"], g["region_id"]) for g in groups)
    needs_cluster = any(v >= 2 for v in rr.values()) and run.task.dedup_llm_cluster
    if not needs_cluster or len(groups) < 2:
        return _create_events(run, bdir, stats, groups, post_ids,
                              {c[0] for c in confirmed})

    # 4) готуємо ОДНУ пачку кластеризації для агента
    rubric_rows = {r.key: r for r in run.task.rubrics.all()}
    reg_name = {}
    # NB: Region імпортовано на рівні модуля — локальний імпорт тут зробив би
    # Region локальною змінною на ВСЮ функцію (UnboundLocalError вище).
    for g in groups:
        if g["region_id"] and g["region_id"] not in reg_name:
            r = Region.objects.filter(id=g["region_id"]).first()
            reg_name[g["region_id"]] = r.name if r else str(g["region_id"])
    incidents = [{"id": i, "rubric": g["rubric"],
                  "region": reg_name.get(g["region_id"], ""),
                  "date": g["date"], "summary": g["summary"]}
                 for i, g in enumerate(groups)]
    with open(f"{bdir}/SYSTEM_PROMPT_CLUSTER.md", "w") as f:
        f.write(run.task.dedup_cluster_prompt or CLUSTER_PROMPT)
    with open(f"{bdir}/cluster.json", "w") as f:
        json.dump({"incidents": incidents}, f, ensure_ascii=False)
    stats.update(phase="cluster", incidents_pre=len(groups))
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("res_runs: run #%s → cluster-фаза (%s пре-інцидентів на злиття)",
             run.id, len(groups))
    return True


def _ingest_cluster(run, bdir, stats) -> bool:
    # чекаємо cluster_done.json від агента
    if not os.path.exists(f"{bdir}/cluster_done.json"):
        return False
    inc = json.load(open(f"{bdir}/_incidents.json"))
    groups = inc["groups"]
    merges = json.load(open(f"{bdir}/cluster_done.json")).get("same_event_groups") or []
    # застосувати злиття: union-find по індексах груп
    parent = list(range(len(groups)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for grp in merges:
        grp = [i for i in grp if isinstance(i, int) and 0 <= i < len(groups)]
        for j in grp[1:]:
            parent[find(j)] = find(grp[0])
    merged = {}
    for i, g in enumerate(groups):
        root = find(i)
        if root not in merged:
            merged[root] = {"rubric": g["rubric"], "region_id": g["region_id"],
                            "date": g["date"], "summary": g["summary"],
                            "member_ids": list(g["member_ids"])}
        else:
            merged[root]["member_ids"].extend(g["member_ids"])
    final = list(merged.values())
    stats.update(incidents_merged=len(final))
    log.info("res_runs: run #%s cluster: %s пре → %s після злиття",
             run.id, len(groups), len(final))
    return _create_events(run, bdir, stats, final,
                          set(inc["post_ids"]), set(inc["confirmed_ids"]))


def _create_events(run, bdir, stats, groups, post_ids, conf_ids) -> bool:
    # NB: Event/Tag імпортовані на рівні модуля — локальний імпорт зробив би їх
    # локальними змінними на ВСЮ функцію (граблі з Region, UnboundLocalError).
    created_event_ids = []
    rubric_rows = {r.key: r for r in run.task.rubrics.all()}
    # ІДЕМПОТЕНТНІСТЬ: прибрати попередні події ЦЬОГО дослідження у вікні запуску,
    # інакше повторний прогін (напр. після правки промпту) подвоїть їх. Скоуп —
    # категорії тегів рубрик задачі (щоб не зачепити чужі події тієї ж задачі).
    cats = {r.tag_category for r in rubric_rows.values() if r.tag_category}
    if cats:
        prev = Event.objects.filter(task=run.task,
                                    event_date__gte=run.date_from,
                                    event_date__lte=run.date_to,
                                    tags__category__in=cats).distinct()
        n_prev = prev.count()
        if n_prev:
            prev.delete()
            log.info("res_runs: run #%s — прибрано %s старих подій перед перегенерацією",
                     run.id, n_prev)
    # відновити обʼєкти постів за id (groups тепер тримають member_ids)
    all_member_ids = [pid for g in groups for pid in g["member_ids"]]
    posts = {p.id: p for p in
             Post.objects.filter(id__in=all_member_ids)
             .select_related("channel", "region_subject")}
    for g in groups:
        g["members"] = [(posts[pid], "") for pid in g["member_ids"] if pid in posts]
    _region_names = dict(Region.objects.values_list("id", "name"))

    # 3) події
    n_ev = 0
    from datetime import date as _date
    for g in groups:
        members = g["members"]
        ps = [m[0] for m in members]
        if not ps:
            continue
        rub = rubric_rows.get(g["rubric"])
        _gdate = _date.fromisoformat(g["date"]) if g.get("date") else None
        ev = Event.objects.create(
            task=run.task,
            event_date=min((p.posted_at.date() for p in ps if p.posted_at), default=_gdate),
            # регіон — З ГРУПИ (вердикт агента), а не з каналу першого поста
            region=(_region_names.get(g["region_id"]) or ""),
            region_subject_id=g["region_id"],
            summary=g["summary"] or (ps[0].text or "")[:300],
            post_count=len(ps),
            channel_count=len({p.channel_id for p in ps if p.channel_id}),
            reach=max(((p.channel.subscribers if p.channel_id and p.channel else 0) or 0)
                      for p in ps),
            review_status=Event.REVIEW_APPROVED,
        )
        if rub:
            tag, _ = Tag.objects.get_or_create(category=rub.tag_category, name=rub.tag_name)
            ev.tags.add(tag)
        # оцінка події = переважна оцінка її постів; при нічиї виграє
        # «критично» (подія, яку хоч частина каналів подала критично, для
        # дослідження цікавіша за нейтральний переказ). NB: частку рахуємо ПО
        # ПОСТАХ — тег на події лише для перегляду й фасетів.
        st = Counter((p.classification or {}).get("stance") for p in ps
                     if (p.classification or {}).get("stance"))
        if st:
            top = max(st.values())
            best = [t for t, c in st.items() if c == top]
            chosen = "критично" if "критично" in best else best[0]
            if s_tag := stance_tag(chosen):
                ev.tags.add(s_tag)
        mk = Counter((p.classification or {}).get("mention_kind") for p in ps
                     if (p.classification or {}).get("mention_kind"))
        if mk and (m_tag := mention_tag(mk.most_common(1)[0][0])):
            ev.tags.add(m_tag)
        for p in ps:
            p.event = ev
            p.is_relevant = True
        created_event_ids.append(ev.id)
        n_ev += 1

    # 4) закрити пости вікна
    all_posts = list(Post.objects.filter(id__in=post_ids))
    for p in all_posts:
        p.is_classified = True
        if p.id not in conf_ids:
            p.is_relevant = False
        p.stage = Post.STAGE_DONE
    Post.objects.bulk_update(all_posts, ["is_classified", "is_relevant", "stage"],
                             batch_size=500)
    Post.objects.bulk_update([p for g in groups for p, _ in g["members"]],
                             ["event", "is_relevant"], batch_size=500)
    stats.update(confirmed=len(conf_ids), incidents=n_ev)

    # опційна стадія: агент-аудит створених подій (симетрично events-конвеєру)
    if run.task.research_audit_enabled and created_event_ids:
        return _prepare_audit(run, bdir, stats, created_event_ids)

    _finish(run, stats)
    return True


def _prepare_audit(run, bdir, stats, event_ids) -> bool:
    """Готує батч агент-аудиту щойно створених подій цього запуску."""
    from analysis.models import Event
    evs = (Event.objects.filter(id__in=event_ids)
           .select_related("region_subject").prefetch_related("tags")
           .order_by("id"))
    items = [{"id": e.id,
              "rubric": ",".join(t.name for t in e.tags.all()),
              "region": e.region or (e.region_subject.name if e.region_subject_id else ""),
              "date": e.event_date.isoformat() if e.event_date else None,
              "summary": e.summary}
             for e in evs]
    prompt = run.task.research_audit_prompt or _default_research_audit_prompt()
    with open(f"{bdir}/SYSTEM_PROMPT_AUDIT.md", "w") as f:
        f.write(prompt)
    with open(f"{bdir}/audit.json", "w") as f:
        json.dump({"items": items}, f, ensure_ascii=False)
    stats.update(phase="audit", audit_events=len(items))
    run.stats = stats
    run.status = "awaiting_agent"
    run.save(update_fields=["stats", "status"])
    log.info("res_runs: run #%s → audit-фаза (%s подій на аудит)",
             run.id, len(items))
    return True


def _ingest_audit(run, bdir, stats) -> bool:
    """Чекає audit_done.json від агента, застосовує keep/reject, тоді фініш."""
    from analysis.models import Event
    if not os.path.exists(f"{bdir}/audit_done.json"):
        return False
    data = json.load(open(f"{bdir}/audit_done.json"))
    n_keep = n_reject = 0
    for it in (data.get("items") or []):
        ev = Event.objects.filter(id=it.get("id"), task=run.task).first()
        if not ev:
            continue
        verdict = (it.get("verdict") or "").lower()
        reason = (it.get("reason") or "")[:200]
        if verdict == "reject":
            ev.review_status = Event.REVIEW_REJECTED
            note = f"[agent review] {reason or 'відхилено агент-аудитом'}"
            ev.review_notes = (ev.review_notes + " | " + note) if ev.review_notes else note
            ev.reviewed_at = djtz.now()
            ev.save(update_fields=["review_status", "review_notes", "reviewed_at"])
            n_reject += 1
        else:
            note = "[agent review] ok" + (f": {reason}" if reason else "")
            ev.review_notes = (ev.review_notes + " | " + note) if ev.review_notes else note
            ev.reviewed_at = djtz.now()
            ev.save(update_fields=["review_notes", "reviewed_at"])
            n_keep += 1
    stats.update(audit_keep=n_keep, audit_reject=n_reject)
    _finish(run, stats)
    return True


def _finish(run, stats):
    from analysis.models import Event
    w = _window_posts(run)
    run.posts_collected = w.count()
    run.posts_relevant = w.filter(is_relevant=True).count()
    run.events_total = Event.objects.filter(
        task=run.task, event_date__gte=run.date_from,
        event_date__lte=run.date_to).count()
    run.stats = stats
    run.status = "done"
    run.finished_at = djtz.now()
    run.save(update_fields=["posts_collected", "posts_relevant", "events_total",
                            "stats", "status", "finished_at"])
    log.info("res_runs: run #%s DONE (кандидатів=%s подій=%s)",
             run.id, stats.get("candidates"), stats.get("incidents"))
