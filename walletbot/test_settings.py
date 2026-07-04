"""
Test-only settings: use SQLite so tests run without a PostgreSQL server.
Usage: DJANGO_SETTINGS_MODULE=walletbot.test_settings python manage.py test ...
"""
from walletbot.settings import *  # noqa: F401, F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",  # noqa: F405
    }
}
