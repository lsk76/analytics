# Корінь pytest (rootdir = backend/). Запуск: docker compose exec web pytest
# Тестова БД: pytest-django створює test_<POSTGRES_DB> у compose-Postgres;
# --reuse-db (pytest.ini) лишає її між запусками, --create-db — перебудувати.
import pytest


@pytest.fixture(autouse=True)
def _no_ssl_redirect(settings):
    """Прод-налаштування редиректять http→https (SECURE_SSL_REDIRECT); тестовий
    клієнт ходить по http://testserver і отримував би 301 замість відповіді."""
    settings.SECURE_SSL_REDIRECT = False
