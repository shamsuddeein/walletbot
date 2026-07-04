"""
Webhook endpoint that receives Helius event notifications.

Design principle: respond to Helius within their timeout (< 2 s) and hand all
real work to Celery.  Never do DB queries or network calls in this view.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .tasks import process_buy_event

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class HeliusWebhookView(View):
    """
    POST /webhook/helius/

    Accepts Helius enhanced-webhook payloads.  Each payload is a JSON array;
    we queue one Celery task per event.
    """

    def post(self, request, *args, **kwargs):
        # ── Optional HMAC signature verification ────────────────────────────
        if settings.HELIUS_WEBHOOK_SECRET:
            sig_header = request.headers.get("Authorization", "")
            expected = hmac.new(
                settings.HELIUS_WEBHOOK_SECRET.encode(),
                request.body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                logger.warning("Helius webhook: invalid signature.")
                return HttpResponseForbidden("Invalid signature")

        # ── Parse body ───────────────────────────────────────────────────────
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            logger.warning("Helius webhook: invalid JSON body.")
            return HttpResponseBadRequest("Invalid JSON")

        # Helius sends an array of events
        events = data if isinstance(data, list) else [data]

        for event in events:
            process_buy_event.delay(event)

        logger.debug("Queued %d event(s) from Helius.", len(events))
        return HttpResponse("OK", status=200)
