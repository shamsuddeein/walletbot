import os
import django
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walletbot.settings")
django.setup()

from walletbot.celery import app as celery_app  # noqa: F401 — ensures Celery is loaded

application = get_wsgi_application()