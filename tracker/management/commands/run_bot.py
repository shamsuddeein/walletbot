"""
Django management command to run the Telegram bot in polling mode.

Usage:
    python manage.py run_bot

On the VPS this is run as a systemd service (see deploy/bot.service).
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Start the Telegram bot in polling mode."

    def handle(self, *args, **options):
        from tracker.telegram_bot import build_application

        self.stdout.write(self.style.SUCCESS("Starting Telegram bot (polling)…"))
        app = build_application()
        app.run_polling(drop_pending_updates=True)
