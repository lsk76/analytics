# Пошук джерел у Telegram — робочі скрипти

Методика й граблі: `docs/tg-channel-discovery.md`.
Конвеєр для постійного моніторингу: `docs/tgsearch-pipeline.md`.

**Чому тут, а не в `_dir/`:** писалися вони як разові й лежали в `backend/_dir/`,
який у `.gitignore` — тобто зникли б із першим `git clean`. Задача повторювана
(зробили прикордонні регіони, потім республіки, Сибір і ДС), тож скрипти
переїхали в репозиторій. Шляхи всередині лишились відносними до `backend/`,
бо запускаються вони звідти.

## Запуск

Django-скрипти виконуються в контейнері, з теки `backend`:

```bash
docker compose exec -T web python manage.py shell < ../tools/discovery/tg_sources/border_resolve.py
```

Простіше — покласти потрібний у `backend/_dir/` і запустити звідти; `_dir`
гітом не відстежується саме для такого чернеткового запуску.

## Що яким

| скрипт | що робить | ключове |
|---|---|---|
| `rep_longlist.py` | лонг-ліст кандидатів із довідника по суб'єктах | junk-регекс, пропуск уже перевірених |
| `border_topic_discovery.py` | глобальний пошук Telegram за ТЕМОЮ | `contacts.Search`, фрази як люди називають групи |
| `border_resolve.py` | resolve + пошук linked-груп, зберігає сирі відповіді | `access_hash`, закріплення акаунта, збої не кешує |
| `border_activity.py` | скільки реально пишуть | РЕАЛЬНИЙ підрахунок, не різниця id |
| `border_tg_topic.py` | пошук слів усередині чатів | Telegram не вміє OR: слово = запит |
| `rep_categorize.py` | категорія джерела через ШІ (новини / міський чат / влада…) | пише в `Channel.topics`, маркер `_cat` |
| `border_import_directory.py` | заливка результату в `Channel` | зіставлення за `tg_id`, потім за юзернеймом |
| `border_export_chats.py` | вивантаження в xlsx | — |

## Порядок

```
rep_longlist (або border_topic_discovery)
   -> border_resolve        # факти: коментарі, linked-групи, учасники
   -> border_import_directory
   -> rep_categorize        # судження: категорія
   -> border_activity       # хто реально живий
   -> border_export_chats
```

Факти (`comments_open`, `linked_chat`) кладуться в довідник один раз; судження
(категорія, придатність) переробляються скільки завгодно разів **без повторного
резолву** — він найдорожчий, ~200 на акаунт за добу.
