# tg-event-analytics

**Перед будь-якою роботою прочитай `docs/AI-GUIDE.md`** — точка входу для ШІ:
модель даних, інваріанти, конвеєри, адмінка, мапа файлів, граблі.

Найкоротше, що треба тримати в голові завжди:

- **Event — єдина одиниця аналітики.** Графіки/матриця читають лише Event.
  Інциденти: N постів → 1 подія (дедуп). Критика (monitor): 1 коментар → 1 подія,
  **БЕЗ дедупу** (лічильник «скільки людей» священний); дзеркало —
  `monitor_stages.sync_comment_event`.
- **Агрегації — тільки через `analysis/services/metrics.py`** (EventSource/PostSource);
  перед `.values().annotate()` завжди `.order_by()`.
- **Гео — `region_subject` FK**, не сирий текст `region` («Саха» матчить «Сахалін»).
- **Нова категорія тегів ⇒ створити рядок `TagCategory`**, інакше адмін-фасет ламається (`?e=1`).
- **Конвеєри НЕ вигадувати** — вони задокументовані: `docs/comments-analysis-pipeline.md`
  (критика), `docs/ethnic-events-pipeline.md` / `docs/econ-events-pipeline.md` (ad-hoc події),
  `docs/ARCHITECTURE.md` (events-воркери). Промпти monitor — `analysis/pilot/prompts.py`.
- **TeleZip:** без негації в запитах; збір по днях/по одному каналу; після падіння VPN —
  `echo "77.88.192.66 api.telezip.net" >> /etc/hosts` у контейнері.
- Зміни коду воркерів/адмінки → `docker compose restart worker-…` / `web`.
- Перед схемними змінами БД — бекап `pg_dump` у `backups/`.
- Адмінка: http://localhost:8001/admin/ (події: `#charts` вбудовані графіки, `matrix/` —
  матриця 8 республік; дефолт списку подій — лише «Схвалено», `?review_status=all` — все).
