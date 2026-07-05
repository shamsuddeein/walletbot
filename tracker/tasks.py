"""
Celery tasks for the wallet tracker.

The webhook view hands work off here immediately and returns 200 to Helius.
All heavy lifting (metadata fetch, DB writes, matching, Telegram alert) happens
inside process_buy_event so Helius never times out waiting for us.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from celery import shared_task
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)


def _parse_buy_from_payload(payload: dict) -> Optional[dict]:
    """
    Extract the fields we need from a Helius enhanced-webhook payload.

    Returns a dict with keys:
        wallet_address, mint, name, symbol, logo_url, amount, timestamp,
        tx_signature, amount_spent, spent_symbol
    or None if this event isn't a buy we care about.

    Helius enhanced SWAP payloads look like:
    {
      "type": "SWAP",
      "source": "RAYDIUM",
      "feePayer": "<wallet address>",
      "timestamp": 1234567890,
      "tokenTransfers": [
        {"toUserAccount": "<wallet>", "mint": "<token mint>", "tokenAmount": 1234},
        ...
      ],
      "nativeTransfers": [...],
      ...
    }
    We match against watched wallets by searching tokenTransfers recipient accounts,
    ensuring we detect swaps routed through Telegram trading bots (Trojan, BonkBot, Maestro).
    """
    event_type = payload.get("type", "")
    if event_type != "SWAP":
        return None

    # Get watched wallets from DB
    from tracker.models import Wallet
    watched_addresses = set(Wallet.objects.values_list("address", flat=True))
    if not watched_addresses:
        return None

    # Find the target token transaction to a watched wallet
    token_transfers = payload.get("tokenTransfers", [])
    target_transfer = None
    buyer_wallet_address = None

    # Exclude base tokens / stablecoins as the bought token
    IGNORED_MINTS = {
        "So11111111111111111111111111111111111111112",  # WSOL/SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }

    for transfer in token_transfers:
        recipient = transfer.get("toUserAccount")
        if recipient in watched_addresses:
            mint = transfer.get("mint")
            if mint in IGNORED_MINTS:
                continue
            target_transfer = transfer
            buyer_wallet_address = recipient
            break

    if not target_transfer or not buyer_wallet_address:
        return None

    bought_mint = target_transfer.get("mint")
    raw_amount = target_transfer.get("tokenAmount")
    try:
        amount = Decimal(str(raw_amount)) if raw_amount is not None else None
    except Exception:
        amount = None

    timestamp_unix = payload.get("timestamp")
    timestamp = (
        datetime.fromtimestamp(timestamp_unix, tz=timezone.utc)
        if timestamp_unix
        else django_tz.now()
    )

    # Now find what this buyer spent
    spent_mint = "So11111111111111111111111111111111111111112"
    spent_amount = Decimal("0.0")
    spent_symbol = "SOL"

    # 1. Check nativeTransfers for spent SOL from the buyer's wallet
    native_transfers = payload.get("nativeTransfers", [])
    for transfer in native_transfers:
        if transfer.get("fromUserAccount") == buyer_wallet_address:
            raw_lamports = transfer.get("amount", 0)
            try:
                spent_amount = Decimal(str(raw_lamports)) / Decimal("1000000000")
            except Exception:
                spent_amount = Decimal("0.0")
            spent_symbol = "SOL"
            break

    # 2. Check tokenTransfers for spent SPL token (e.g. USDC, WSOL) if native SOL spent is 0
    if spent_amount == Decimal("0.0"):
        for transfer in token_transfers:
            if transfer.get("fromUserAccount") == buyer_wallet_address:
                mint = transfer.get("mint")
                if mint != bought_mint:
                    raw_amount = transfer.get("tokenAmount")
                    try:
                        spent_amount = Decimal(str(raw_amount)) if raw_amount is not None else Decimal("0.0")
                    except Exception:
                        spent_amount = Decimal("0.0")
                    spent_mint = mint
                    spent_symbol = transfer.get("tokenSymbol", "TOKEN")
                    break

    tx_signature = payload.get("signature", "")

    return {
        "wallet_address": buyer_wallet_address,
        "mint": bought_mint,
        "name": target_transfer.get("tokenName", ""),
        "symbol": target_transfer.get("tokenSymbol", ""),
        "logo_url": target_transfer.get("tokenIcon", ""),
        "amount": amount,
        "timestamp": timestamp,
        "tx_signature": tx_signature,
        "amount_spent": spent_amount,
        "spent_symbol": spent_symbol,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_buy_event(self, payload: dict):
    """
    Main processing task.  Called by the webhook view for every Helius event.

    Steps:
      1. Parse the payload — bail if it's not a buy we care about.
      2. Confirm the wallet is one we're watching.
      3. Check for transaction idempotency to avoid duplicates.
      4. Fetch missing token metadata from Helius if needed.
      5. Compute the logo perceptual hash.
      6. Save the TokenBuy to the database.
      7. Run all three match checks against this wallet's past buys.
      8. For each match: save a MatchAlert and send a Telegram alert.
    """
    from django.conf import settings
    from tracker.models import Wallet, TokenBuy, MatchAlert
    from tracker import helius as helius_api
    from tracker.matching import run_all_checks, compute_logo_hash
    from tracker.telegram_bot import send_alert, format_time_diff
    from tracker.ai import get_ai_explanation, get_token_risk, get_wallet_context

    try:
        buy_data = _parse_buy_from_payload(payload)
        if buy_data is None:
            logger.debug("Payload is not a trackable buy — skipping.")
            return

        # Only process wallets we're watching
        try:
            wallet = Wallet.objects.get(address=buy_data["wallet_address"])
        except Wallet.DoesNotExist:
            logger.debug("Wallet %s not in watch list — skipping.", buy_data["wallet_address"])
            return

        # Check if transaction has already been processed to ensure idempotency
        tx_sig = buy_data.get("tx_signature")
        if tx_sig:
            if TokenBuy.objects.filter(tx_signature=tx_sig).exists():
                logger.info("Transaction %s already processed — skipping.", tx_sig)
                return

        # Fetch missing metadata from Helius if the payload didn't include it
        if not buy_data["name"] or not buy_data["symbol"]:
            meta = helius_api.get_token_metadata(buy_data["mint"])
            buy_data["name"] = buy_data["name"] or meta["name"]
            buy_data["symbol"] = buy_data["symbol"] or meta["symbol"]
            buy_data["logo_url"] = buy_data["logo_url"] or meta["logo_url"]

        # Compute logo hash now, store it so future comparisons are instant
        logo_hash = compute_logo_hash(buy_data["logo_url"]) if buy_data["logo_url"] else ""

        # Save the buy
        new_buy = TokenBuy.objects.create(
            wallet=wallet,
            name=buy_data["name"],
            symbol=buy_data["symbol"],
            logo_url=buy_data["logo_url"],
            logo_hash=logo_hash or "",
            contract_address=buy_data["mint"],
            amount=buy_data["amount"],
            timestamp=buy_data["timestamp"],
            tx_signature=tx_sig,
            amount_spent=buy_data["amount_spent"],
            spent_symbol=buy_data["spent_symbol"],
            raw_payload=payload,
        )

        # Get all past buys for this wallet (excluding the one we just saved)
        past_buys = TokenBuy.objects.filter(wallet=wallet).exclude(pk=new_buy.pk)

        # Score token risk using live DexScreener data + AI
        token_risk = get_token_risk(
            name=new_buy.name or "Unknown",
            symbol=new_buy.symbol or "?",
            contract_address=new_buy.contract_address,
        )

        wallet_context = ""
        # Build wallet context from recent history (only if AI is enabled)
        if settings.OPENROUTER_API_KEY:
            recent_buys_data = [
                {
                    "name": b.name or "?",
                    "symbol": b.symbol or "?",
                    "timestamp_str": b.timestamp.strftime("%b %d"),
                }
                for b in past_buys.order_by("-timestamp")[:15]
            ]
            wallet_context = get_wallet_context(
                wallet_nickname=wallet.nickname,
                recent_buys=recent_buys_data,
            )

        # Run matching
        matches = run_all_checks(new_buy, past_buys)

        for match_result in matches:
            try:
                matched_buy = TokenBuy.objects.get(pk=match_result.matched_buy_id)
            except TokenBuy.DoesNotExist:
                continue

            alert = MatchAlert.objects.create(
                new_buy=new_buy,
                matched_buy=matched_buy,
                match_type=match_result.match_type,
                name_score=match_result.name_score,
                symbol_score=match_result.symbol_score,
                logo_distance=match_result.logo_distance,
            )

            time_diff = format_time_diff(new_buy.timestamp, matched_buy.timestamp)

            match_parts = []
            if match_result.name_score is not None:
                if match_result.name_score >= settings.NAME_MATCH_THRESHOLD:
                    match_parts.append(f"similar name ({match_result.name_score:.0f}%)")
            if match_result.symbol_score is not None:
                if match_result.symbol_score >= settings.SYMBOL_MATCH_THRESHOLD:
                    match_parts.append(f"similar symbol ({match_result.symbol_score:.0f}%)")
            if match_result.logo_distance is not None:
                if match_result.logo_distance <= settings.LOGO_MATCH_THRESHOLD:
                    match_parts.append("similar logo")
            match_reason = " and ".join(match_parts) if match_parts else match_result.match_type

            ai_explanation = ""
            if settings.OPENROUTER_API_KEY:
                ai_explanation = get_ai_explanation(
                    new_name=new_buy.name or "Unknown",
                    new_symbol=new_buy.symbol or "?",
                    past_name=matched_buy.name or "Unknown",
                    past_symbol=matched_buy.symbol or "?",
                    time_diff=time_diff,
                    match_reason=match_reason,
                    wallet_nickname=wallet.nickname,
                )

            send_alert(
                alert,
                ai_explanation=ai_explanation,
                token_risk=token_risk,
                wallet_context=wallet_context,
            )

            logger.info(
                "Alert sent: %s matched %s via %s",
                new_buy,
                matched_buy,
                match_result.match_type,
            )

        # DEBUG mode: notify if a buy was successfully processed but no matches were found
        from django.conf import settings
        if not matches and settings.DEBUG:
            from tracker.telegram_bot import _send_message, _get_allowed_user_id
            chat_id = _get_allowed_user_id()
            if chat_id != 0:
                _send_message(
                    chat_id,
                    f"ℹ️ <b>Buy Processed:</b> {new_buy.name or '?'} ({new_buy.symbol or '?'})\n"
                    f"Wallet: {wallet.nickname}\n"
                    f"Compared against {past_buys.count()} past buys. No similar tokens found.",
                    parse_mode="HTML"
                )

    except Exception as exc:
        logger.exception("process_buy_event failed: %s", exc)
        raise self.retry(exc=exc)


@shared_task
def daily_digest():
    """
    Runs every morning at 9am.
    Summarises the last 24 hours of wallet activity and sends it via Telegram.
    Only sends if there was at least one buy or alert in the past 24 hours.
    """
    from datetime import timedelta
    from django.utils import timezone as django_tz
    from tracker.models import Wallet, TokenBuy, MatchAlert
    from tracker.ai import generate_daily_digest
    from tracker.telegram_bot import _send_message, _get_allowed_user_id

    since = django_tz.now() - timedelta(hours=24)
    wallets = Wallet.objects.all()
    summaries = []

    for wallet in wallets:
        buys = TokenBuy.objects.filter(wallet=wallet, timestamp__gte=since)
        alerts = MatchAlert.objects.filter(new_buy__wallet=wallet, sent_at__gte=since)
        buy_count = buys.count()
        alert_count = alerts.count()

        if buy_count == 0:
            continue

        token_names = list(buys.values_list("name", flat=True).distinct()[:5])
        summaries.append({
            "nickname": wallet.nickname,
            "buys_24h": buy_count,
            "alerts_24h": alert_count,
            "tokens": token_names,
        })

    if not summaries:
        logger.info("daily_digest: no activity in last 24h, skipping.")
        return

    digest_text = generate_daily_digest(summaries)
    if not digest_text:
        logger.warning("daily_digest: AI returned empty text.")
        return

    chat_id = _get_allowed_user_id()
    if chat_id:
        _send_message(chat_id, f"Good morning.\n\n{digest_text}")
        logger.info("daily_digest sent.")


@shared_task
def wallet_anomaly_check():
    """
    Runs every hour.
    If any wallet buys 3+ tokens in under 2 hours, sends an alert.
    """
    from datetime import timedelta
    from django.utils import timezone as django_tz
    from tracker.models import Wallet, TokenBuy
    from tracker.telegram_bot import _send_message, _get_allowed_user_id

    since = django_tz.now() - timedelta(hours=2)
    wallets = Wallet.objects.all()
    chat_id = _get_allowed_user_id()

    for wallet in wallets:
        # Prevent duplicate alert spam by enforcing a 2-hour cooldown
        if wallet.last_anomaly_alert_sent and wallet.last_anomaly_alert_sent >= since:
            logger.info("Anomaly check: wallet %s already alerted recently — skipping.", wallet.nickname)
            continue

        recent_count = TokenBuy.objects.filter(wallet=wallet, timestamp__gte=since).count()
        if recent_count >= 3:
            _send_message(
                chat_id,
                f"Unusual activity: {wallet.nickname} has made {recent_count} buys in the last 2 hours.\n"
                f"This is higher than normal. Could be a coordinated move — worth watching closely."
            )
            # Update last anomaly alert sent timestamp
            wallet.last_anomaly_alert_sent = django_tz.now()
            wallet.save(update_fields=["last_anomaly_alert_sent"])
            logger.info("Anomaly alert sent for wallet %s (%d buys in 2h)", wallet.nickname, recent_count)
