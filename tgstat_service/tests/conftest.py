import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_configure(config):
    # Кожен тест API може підсунути свій підставний браузер:
    # @with_browser(ApiBrowser(...)) — це і є ця мітка.
    config.addinivalue_line("markers", "browser(obj): підставний браузер для фікстури api")
