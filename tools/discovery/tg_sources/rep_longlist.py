"""Лонг-ліст кандидатів по республіках, Сибіру й Далекому Сходу.

Той самий принцип, що й у прикордонній роботі, але БЕЗ прив'язки до міст:
одиниця гео тут — суб'єкт. Уже перевірені (comments_open не NULL) пропускаються:
резолв дорогий, платити за нього двічі немає сенсу.
"""
import json, re
from pathlib import Path
from analysis.models import Channel, Region

REGIONS = ["Дагестан", "Башкортостан", "Саха (Якутія)", "Бурятія",
           "Алтай", "Алтайський край", "Іркутська область", "Кемеровська область",
           "Красноярський край", "Новосибірська область", "Омська область",
           "Томська область", "Тива", "Хакасія",
           "Амурська область", "Забайкальський край", "Камчатський край",
           "Магаданська область", "Приморський край", "Сахалінська область",
           "Хабаровський край", "Єврейська АО", "Чукотський АО"]
SIBERIA = {"Алтай", "Алтайський край", "Іркутська область", "Кемеровська область",
           "Красноярський край", "Новосибірська область", "Омська область",
           "Томська область", "Тива", "Хакасія"}
FAREAST = {"Амурська область", "Бурятія", "Забайкальський край", "Камчатський край",
           "Магаданська область", "Приморський край", "Саха (Якутія)",
           "Сахалінська область", "Хабаровський край", "Єврейська АО", "Чукотський АО"}
JUNK = re.compile(
    r"барахолк|куплю|продам|объявлени|доск[аи]\s|работа|ваканси|подработ|"
    r"знакомств|такси|доставк|аренд|недвижим|квартир|авто|дром|радар|дпс|"
    r"тур[ыи]\b|путешеств|попутчик|отдам|даром|услуг|строит|ремонт|мебел|"
    r"мода|одежд|крипт|ставк|казино|букмекер|игр[ыао]|гейм|аниме|мем|"
    r"знайомств|секс|интим|18\+|флудилк|музык|песн|ырылар", re.I)

def macro(name):
    if name in ("Дагестан", "Башкортостан"):
        return "республіка"
    if name in SIBERIA:
        return "Сибір"
    return "Далекий Схід"

ids = dict(Region.objects.filter(name__in=REGIONS).values_list("name", "id"))
rows = []
for ch in (Channel.objects.filter(region_subject_id__in=ids.values())
           .exclude(username="").select_related("region_subject")):
    text = f"{ch.title or ''} {ch.description or ''}"
    rows.append({
        "id": ch.id, "tg_id": ch.tg_id, "username": ch.username,
        "title": ch.title or "", "description": (ch.description or "")[:400],
        "subscribers": ch.subscribers, "region": ch.region_subject.name,
        "region_id": ch.region_subject_id, "macro": macro(ch.region_subject.name),
        "chat_type": ch.chat_type or "unknown",
        "is_invite": ch.username.startswith("+"),
        "already_checked": ch.comments_open is not None,
        "junk_hint": bool(JUNK.search(text)),
    })
Path("_dir/rep_longlist.json").write_text(json.dumps({"items": rows}, ensure_ascii=False, indent=1))

clean = [r for r in rows if not r["junk_hint"] and not r["is_invite"]]
todo = [r for r in clean if not r["already_checked"]]
print(f"усього в довіднику: {len(rows)}")
print(f"після junk-фільтра й без інвайтів: {len(clean)}")
print(f"ЩЕ НЕ ПЕРЕВІРЕНО на коментарі: {len(todo)}\n")
import collections
for m in ("республіка", "Сибір", "Далекий Схід"):
    sel = [r for r in todo if r["macro"] == m]
    print(f"{m:<16}{len(sel):>5}")
    for reg, n in collections.Counter(r["region"] for r in sel).most_common(4):
        print(f"    {reg[:26]:<28}{n}")
