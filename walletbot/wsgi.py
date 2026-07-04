import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walletbot.settings")
django.setup()

from walletbot.celery import app as celery_app  # noqa: F401 — ensures Celery is loaded

application = django.core.handlers.wsgi.WSGIHandler()
