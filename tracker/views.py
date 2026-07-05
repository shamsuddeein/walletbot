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
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required

import pandas as pd

from .tasks import process_buy_event
from .models import TokenBuy

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

@staff_member_required
def admin_analytics_view(request):
    buys = TokenBuy.objects.select_related("wallet").all().values(
        "wallet__nickname", "name", "symbol", "amount", "amount_spent", "spent_symbol", "timestamp"
    )

    context = {
        "wallet_stats": None,
        "top_tokens": None,
        "daily_stats": None,
    }

    if buys.exists():
        df = pd.DataFrame(list(buys))

        # 1. Wallet stats
        wallet_stats_df = df.groupby("wallet__nickname").agg(
            total_swaps=("amount", "count"),
            total_spent=("amount_spent", "sum"),
        ).reset_index()
        wallet_stats_df.columns = ["Wallet Nickname", "Total Swaps", "Total Spent"]
        wallet_stats_df["Total Spent"] = wallet_stats_df["Total Spent"].map(lambda x: f"{x:,.3f} SOL" if pd.notnull(x) else "0.000 SOL")
        context["wallet_stats"] = wallet_stats_df.to_html(classes="analytics-table", index=False)

        # 2. Top tokens
        top_tokens_df = df.groupby(["name", "symbol"]).size().reset_index(name="Total Swaps")
        top_tokens_df = top_tokens_df.sort_values("Total Swaps", ascending=False).head(5)
        top_tokens_df.columns = ["Token Name", "Symbol", "Total Swaps"]
        context["top_tokens"] = top_tokens_df.to_html(classes="analytics-table", index=False)

        # 3. Daily volume
        df["date"] = pd.to_datetime(df["timestamp"]).dt.date
        daily_stats_df = df.groupby("date").size().reset_index(name="Total Swaps").sort_values("date", ascending=False)
        daily_stats_df.columns = ["Date", "Total Swaps"]
        context["daily_stats"] = daily_stats_df.to_html(classes="analytics-table", index=False)

    return render(request, "admin/analytics.html", context)
