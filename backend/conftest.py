# Корінь pytest (rootdir = backend/). Запуск: docker compose exec web pytest
# Тестова БД: pytest-django створює test_<POSTGRES_DB> у compose-Postgres;
# --reuse-db (pytest.ini) лишає її між запусками, --create-db — перебудувати.
