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


def _parse_buys_from_payload(payload: dict, watched_addresses: set[str] | None = None) -> list[dict]:
    """
    Extract the fields we need from a Helius enhanced-webhook payload.
    Returns a list of dicts (one for each matched watched wallet in the transaction)
    with keys: wallet_address, mint, name, symbol, logo_url, amount, timestamp,
    tx_signature, amount_spent, spent_symbol.
    """
    event_type = payload.get("type", "")
    if event_type != "SWAP":
        return []

    # Get watched wallets from DB
    if watched_addresses is None:
        from tracker.models import Wallet
        watched_addresses = set(Wallet.objects.values_list("address", flat=True))
    if not watched_addresses:
        return []

    token_transfers = payload.get("tokenTransfers", [])
    if not token_transfers:
        return []

    # Exclude base tokens / stablecoins as the bought token
    IGNORED_MINTS = {
        "So11111111111111111111111111111111111111112",  # WSOL/SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }

    timestamp_unix = payload.get("timestamp")
    if timestamp_unix:
        timestamp = (
            datetime.fromtimestamp(timestamp_unix, tz=timezone.utc)
            if django_tz.is_aware(django_tz.now())
            else datetime.fromtimestamp(timestamp_unix)
        )
    else:
        timestamp = django_tz.now()

    tx_signature = payload.get("signature") or None

    buys = []
    # Identify all token transfers going to any watched address
    for transfer in token_transfers:
        recipient = transfer.get("toUserAccount")
        if recipient in watched_addresses:
            mint = transfer.get("mint")
            if mint in IGNORED_MINTS:
                continue

            raw_amount = transfer.get("tokenAmount")
            try:
                amount = Decimal(str(raw_amount)) if raw_amount is not None else None
            except Exception:
                amount = None

            # Now find what this specific recipient spent in this transaction
            spent_amount = Decimal("0.0")
            spent_symbol = "SOL"

            native_transfers = payload.get("nativeTransfers", [])
            for native_tx in native_transfers:
                if native_tx.get("fromUserAccount") == recipient:
                    raw_lamports = native_tx.get("amount", 0)
                    try:
                        spent_amount = Decimal(str(raw_lamports)) / Decimal("1000000000")
                    except Exception:
                        spent_amount = Decimal("0.0")
                    spent_symbol = "SOL"
                    break

            if spent_amount == Decimal("0.0"):
                for token_tx in token_transfers:
                    if token_tx.get("fromUserAccount") == recipient:
                        tok_mint = token_tx.get("mint")
                        if tok_mint != mint:
                            tok_raw_amount = token_tx.get("tokenAmount")
                            try:
                                spent_amount = Decimal(str(tok_raw_amount)) if tok_raw_amount is not None else Decimal("0.0")
                            except Exception:
                                spent_amount = Decimal("0.0")
                            raw_sym = token_tx.get("tokenSymbol")
                            if tok_mint == "So11111111111111111111111111111111111111112":
                                spent_symbol = "SOL"
                            else:
                                spent_symbol = raw_sym or "TOKEN"
                            break

            buys.append({
                "wallet_address": recipient,
                "mint": mint,
                "name": transfer.get("tokenName", ""),
                "symbol": transfer.get("tokenSymbol", ""),
                "logo_url": transfer.get("tokenIcon", ""),
                "amount": amount,
                "timestamp": timestamp,
                "tx_signature": tx_signature,
                "amount_spent": spent_amount,
                "spent_symbol": spent_symbol,
            })

    return buys


@shared_task(bind=True, queue="live_alerts", max_retries=3, default_retry_delay=30)
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
    from datetime import timedelta

    try:
        buy_events = _parse_buys_from_payload(payload)
        if not buy_events:
            logger.debug("Payload has no trackable buys — skipping.")
            return

        for buy_data in buy_events:
            # Only process wallets we're watching
            try:
                wallet = Wallet.objects.get(address=buy_data["wallet_address"])
            except Wallet.DoesNotExist:
                logger.debug("Wallet %s not in watch list — skipping.", buy_data["wallet_address"])
                continue

            # Check if transaction has already been processed for this wallet to ensure idempotency
            tx_sig = buy_data.get("tx_signature")
            if tx_sig:
                if TokenBuy.objects.filter(wallet=wallet, tx_signature=tx_sig).exists():
                    logger.info("Transaction %s already processed for wallet %s — skipping.", tx_sig, wallet.nickname)
                    continue

            # Check for near-duplicate by wallet, contract_address, and timestamp within 5 seconds
            time_min = buy_data["timestamp"] - timedelta(seconds=5)
            time_max = buy_data["timestamp"] + timedelta(seconds=5)
            if TokenBuy.objects.filter(
                wallet=wallet,
                contract_address=buy_data["mint"],
                timestamp__range=(time_min, time_max)
            ).exists():
                logger.info(
                    "A TokenBuy for wallet %s, mint %s around timestamp %s already exists (5s window) — skipping.",
                    wallet.address,
                    buy_data["mint"],
                    buy_data["timestamp"]
                )
                continue

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

            # Score token risk using live DexScreener data (instant, rules-based)
            token_risk = get_token_risk(
                name=new_buy.name or "Unknown",
                symbol=new_buy.symbol or "?",
                contract_address=new_buy.contract_address,
                amount_spent=new_buy.amount_spent,
                amount_received=new_buy.amount,
                spent_symbol=new_buy.spent_symbol,
            )

            # Run matching
            matches = run_all_checks(new_buy, past_buys)

            # Only process the highest scoring match if multiple exist
            if matches:
                matches.sort(key=lambda m: max(m.name_score or 0.0, m.symbol_score or 0.0), reverse=True)
                best_match = matches[0]

                try:
                    matched_buy = TokenBuy.objects.get(pk=best_match.matched_buy_id)
                except TokenBuy.DoesNotExist:
                    continue

                alert = MatchAlert.objects.create(
                    new_buy=new_buy,
                    matched_buy=matched_buy,
                    match_type=best_match.match_type,
                    name_score=best_match.name_score,
                    symbol_score=best_match.symbol_score,
                    logo_distance=best_match.logo_distance,
                )

                # Check if this exact pair has already been alerted for this wallet before
                already_alerted = MatchAlert.objects.filter(
                    new_buy__wallet=wallet,
                    new_buy__contract_address=new_buy.contract_address,
                    matched_buy__contract_address=matched_buy.contract_address,
                ).exclude(pk=alert.pk).exists()

                if already_alerted:
                    logger.info(
                        "MatchAlert saved to database, but Telegram alert skipped (already sent before for this pair: new_buy=%s, matched_buy=%s).",
                        new_buy.contract_address,
                        matched_buy.contract_address,
                    )
                    continue

                send_alert(
                    alert,
                    token_risk=token_risk,
                )

                logger.info(
                    "Alert sent: %s matched %s via %s",
                    new_buy,
                    matched_buy,
                    best_match.match_type,
                )

            # DEBUG mode: notify if a buy was successfully processed but no matches were found
            if not matches and settings.DEBUG:
                from tracker.telegram_bot import _send_message, _get_allowed_user_ids
                chat_ids = _get_allowed_user_ids()
                for chat_id in chat_ids:
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


@shared_task(queue="default")
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
    from tracker.telegram_bot import _send_message, _get_allowed_user_ids

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

    chat_ids = _get_allowed_user_ids()
    for chat_id in chat_ids:
        if chat_id:
            _send_message(chat_id, f"Good morning.\n\n{digest_text}")
    logger.info("daily_digest sent to all allowed users.")


@shared_task(queue="default")
def wallet_anomaly_check():
    """
    Runs every hour.
    If any wallet buys 3+ tokens in under 2 hours, sends an alert.
    """
    from datetime import timedelta
    from django.utils import timezone as django_tz
    from tracker.models import Wallet, TokenBuy
    from tracker.telegram_bot import _send_message, _get_allowed_user_ids

    since = django_tz.now() - timedelta(hours=2)
    wallets = Wallet.objects.all()
    chat_ids = _get_allowed_user_ids()

    for wallet in wallets:
        # Prevent duplicate alert spam by enforcing a 2-hour cooldown
        if wallet.last_anomaly_alert_sent and wallet.last_anomaly_alert_sent >= since:
            logger.info("Anomaly check: wallet %s already alerted recently — skipping.", wallet.nickname)
            continue

        recent_count = TokenBuy.objects.filter(wallet=wallet, timestamp__gte=since).count()
        if recent_count >= 3:
            for chat_id in chat_ids:
                _send_message(
                    chat_id,
                    f"Unusual activity: {wallet.nickname} has made {recent_count} buys in the last 2 hours.\n"
                    f"This is higher than normal. Could be a coordinated move — worth watching closely."
                )
            # Update last anomaly alert sent timestamp
            wallet.last_anomaly_alert_sent = django_tz.now()
            wallet.save(update_fields=["last_anomaly_alert_sent"])
            logger.info("Anomaly alert sent for wallet %s (%d buys in 2h)", wallet.nickname, recent_count)


@shared_task(queue="backfills")
def backfill_wallet_history_task(address: str, nickname: str, chat_id: int):
    """
    Backfill wallet transaction history.
    Limits data to BACKFILL_DAYS and caps total transactions at BACKFILL_MAX_TRANSACTIONS.
    Fails gracefully and notifies the user of the result.
    """
    import requests
    import time
    from datetime import datetime, timezone, timedelta
    from django.conf import settings
    from django.utils import timezone as django_tz
    from tracker.models import Wallet, TokenBuy
    from tracker.matching import compute_logo_hash
    from tracker.telegram_bot import _send_message
    from tracker import helius as helius_api

    backfill_days = getattr(settings, "BACKFILL_DAYS", 30)
    max_transactions = getattr(settings, "BACKFILL_MAX_TRANSACTIONS", 200)
    cutoff_time = django_tz.now() - timedelta(days=backfill_days)
    
    api_key = settings.HELIUS_API_KEY
    if not api_key:
        logger.warning("Helius API key missing; skipping backfill.")
        _send_message(chat_id, f"⚠️ I couldn't load past history for {nickname}, but live tracking is active.", parse_mode="HTML")
        return

    try:
        wallet = Wallet.objects.get(address=address)
    except Wallet.DoesNotExist:
        logger.warning("Wallet %s was removed before backfill started.", address)
        return

    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    params = {"api-key": api_key, "type": "SWAP"}
    before_sig = None
    success = True
    fetched_count = 0
    watched_addresses = {address}

    try:
        while True:
            if before_sig:
                params["before"] = before_sig
            
            # Fetch parsed history page
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            txs = r.json()

            if not txs:
                break

            last_sig = None
            reached_cutoff = False

            for tx in txs:
                fetched_count += 1
                if fetched_count > max_transactions:
                    logger.info("Reached maximum transaction cap of %d — stopping backfill.", max_transactions)
                    reached_cutoff = True
                    break

                last_sig = tx.get("signature")
                timestamp_unix = tx.get("timestamp")
                if not timestamp_unix:
                    continue
                
                tx_time = (
                    datetime.fromtimestamp(timestamp_unix, tz=timezone.utc)
                    if django_tz.is_aware(cutoff_time)
                    else datetime.fromtimestamp(timestamp_unix)
                )
                if tx_time < cutoff_time:
                    reached_cutoff = True
                    break

                # Skip if signature exists to avoid duplicates
                tx_sig = tx.get("signature")
                if tx_sig and TokenBuy.objects.filter(tx_signature=tx_sig).exists():
                    continue

                # Parse transaction
                buy_data = _parse_buy_from_payload(tx, watched_addresses=watched_addresses)
                if not buy_data:
                    continue

                # Check for near-duplicate by wallet, contract_address, and timestamp within 5 seconds
                time_min = buy_data["timestamp"] - timedelta(seconds=5)
                time_max = buy_data["timestamp"] + timedelta(seconds=5)
                if TokenBuy.objects.filter(
                    wallet=wallet,
                    contract_address=buy_data["mint"],
                    timestamp__range=(time_min, time_max)
                ).exists():
                    logger.info(
                        "Backfill: A TokenBuy for wallet %s, mint %s around timestamp %s already exists (5s window) — skipping.",
                        wallet.address,
                        buy_data["mint"],
                        buy_data["timestamp"]
                    )
                    continue

                # Fetch missing metadata
                if not buy_data["name"] or not buy_data["symbol"]:
                    meta = helius_api.get_token_metadata(buy_data["mint"])
                    buy_data["name"] = buy_data["name"] or meta["name"]
                    buy_data["symbol"] = buy_data["symbol"] or meta["symbol"]
                    buy_data["logo_url"] = buy_data["logo_url"] or meta["logo_url"]

                # Compute logo hash
                logo_hash = compute_logo_hash(buy_data["logo_url"]) if buy_data["logo_url"] else ""

                # Save record (without triggers)
                TokenBuy.objects.create(
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
                    raw_payload=tx,
                )

            if reached_cutoff or not last_sig:
                break
            
            before_sig = last_sig
            
            # Rate limit protection: pause between pages
            time.sleep(0.2)

    except Exception as exc:
        logger.exception("Error backfilling wallet %s: %s", address, exc)
        success = False

    # Send result to user
    if success:
        _send_message(
            chat_id,
            f"📥 I've also loaded this wallet's last <b>{backfill_days}</b> days of buy history (capped at {max_transactions} txs), "
            f"so future matches can be checked against it.",
            parse_mode="HTML"
        )
    else:
        _send_message(
            chat_id,
            f"⚠️ I couldn't load past history for {nickname}, but live tracking is active.",
            parse_mode="HTML"
        )
