"""
Telegram bot: command handlers + alert sender.

Commands (only the configured TELEGRAM_ALLOWED_USER_ID may run them):
  /start          — welcome message
  /addwallet      — add a wallet to the watch list (alias: /add wallet)
  /removewallet   — remove a wallet (alias: /remove wallet)
  /listwallets    — show all tracked wallets (alias: /list wallets, /list)
  /claim          — first-run: claim ownership if no owner is set yet

Alert sending is done via send_alert(), called from Celery tasks, so it
runs outside the bot's event loop using the Bot.send_message() HTTP API
directly (no event loop required).
"""
from __future__ import annotations

import logging
import os
import re
from functools import wraps
from pathlib import Path

import requests
from django.conf import settings
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# Valid Solana base58 address: 32–44 chars, no 0/O/I/l
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_aware(dt):
    """Ensure naive datetimes are converted to timezone-aware using the default timezone."""
    from django.utils import timezone as django_tz
    if dt and django_tz.is_naive(dt):
        return django_tz.make_aware(dt, django_tz.get_default_timezone())
    return dt


def _send_message(chat_id: int | str, text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via the Telegram Bot API HTTP endpoint."""
    try:
        r = requests.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", exc)
        return False


def _get_allowed_user_id() -> int:
    """
    Return the current allowed user ID.
    Re-reads from environment each call so changes written to .env
    by /claim are picked up without restarting the bot.
    """
    env_path = Path(settings.BASE_DIR) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_ALLOWED_USER_ID="):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return settings.TELEGRAM_ALLOWED_USER_ID


def _write_allowed_user_id(user_id: int) -> bool:
    """Patch TELEGRAM_ALLOWED_USER_ID in the .env file at runtime."""
    env_path = Path(settings.BASE_DIR) / ".env"
    if not env_path.exists():
        return False
    content = env_path.read_text()
    new_content = re.sub(
        r"^TELEGRAM_ALLOWED_USER_ID=.*$",
        f"TELEGRAM_ALLOWED_USER_ID={user_id}",
        content,
        flags=re.MULTILINE,
    )
    env_path.write_text(new_content)
    return True


# ── Database Async Helpers ───────────────────────────────────────────────────

@sync_to_async
def db_add_wallet(address: str, nickname: str, user_id: int) -> str:
    from tracker.models import Wallet
    if Wallet.objects.count() >= settings.MAX_WALLETS:
        return "limit_exceeded"
    if Wallet.objects.filter(address=address).exists():
        return "already_exists_address"
    if Wallet.objects.filter(nickname__iexact=nickname).exists():
        return "already_exists_nickname"
    Wallet.objects.create(
        address=address,
        nickname=nickname,
        added_by_telegram_id=user_id,
    )
    return "ok"


@sync_to_async
def db_remove_wallet(query: str) -> tuple[str, str] | None:
    from tracker.models import Wallet
    wallet = (
        Wallet.objects.filter(address=query).first()
        or Wallet.objects.filter(nickname__iexact=query).first()
    )
    if not wallet:
        return None
    address = wallet.address
    nickname = wallet.nickname
    wallet.delete()
    return address, nickname


@sync_to_async
def db_list_wallets() -> list[dict]:
    from tracker.models import Wallet
    return list(Wallet.objects.values("nickname", "address", "date_added"))


# ── Access control ────────────────────────────────────────────────────────────

def owner_only(handler):
    """
    Decorator: only the configured owner may run this command.

    Smart behaviour:
    - If no owner is set (ID = 0), suggests /claim.
    - If owner is set but doesn't match, shows the sender's ID so they
      know exactly what to fix — no digging through config files.
    """
    @wraps(handler)
    async def wrapper(update, context):
        user_id = update.effective_user.id
        allowed = _get_allowed_user_id()

        if allowed == 0:
            await update.message.reply_text(
                "No owner configured yet. Send claim to claim this bot as yours.",
                parse_mode="",
            )
            return

        if user_id != allowed:
            await update.message.reply_text(
                "You are not authorized to use this bot. Please contact the administrator to grant access for your account.",
                parse_mode="",
            )
            return

        return await handler(update, context)
    return wrapper


# ── Alert formatter ───────────────────────────────────────────────────────────

def format_time_diff(t1, t2) -> str:
    diff = abs(t1 - t2)
    days = diff.days
    hours = diff.seconds // 3600
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0 or not parts:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    return ", ".join(parts)


def send_alert(alert, ai_explanation: str = "", token_risk: dict | None = None, wallet_context: str = "") -> bool:
    from django.utils import timezone as django_tz

    new = alert.new_buy
    past = alert.matched_buy
    wallet = new.wallet

    # Format localized timestamps
    local_new = django_tz.localtime(_make_aware(new.timestamp))
    local_past = django_tz.localtime(_make_aware(past.timestamp))
    new_time = local_new.strftime("%B %d, %Y, %I:%M %p")
    past_time = local_past.strftime("%B %d, %Y, %I:%M %p")

    # Calculate difference
    time_diff = format_time_diff(new.timestamp, past.timestamp)

    # Format match reason
    match_parts = []
    if alert.name_score is not None and alert.name_score >= settings.NAME_MATCH_THRESHOLD:
        match_parts.append(f"similar name ({alert.name_score:.0f}%)")
    if alert.symbol_score is not None and alert.symbol_score >= settings.SYMBOL_MATCH_THRESHOLD:
        match_parts.append(f"similar symbol ({alert.symbol_score:.0f}%)")
    if alert.logo_distance is not None and alert.logo_distance <= settings.LOGO_MATCH_THRESHOLD:
        match_parts.append("similar logo")

    match_reason = " and ".join(match_parts) if match_parts else alert.match_type

    # Format URL links
    dex_url = f"https://dexscreener.com/solana/{new.contract_address}"
    solscan_url = f"https://solscan.io/token/{new.contract_address}"

    # Build the message
    text = (
        f"The wallet named {wallet.nickname} just bought a token similar to one it bought before.\n\n"
        f"New token: {new.name or '?'} ({new.symbol or '?'})\n"
        f"Bought: {new_time}\n\n"
        f"Matched with: {past.name or '?'} ({past.symbol or '?'})\n"
        f"Bought: {past_time}\n\n"
        f"Time between buys: {time_diff}\n\n"
        f"Match reason: {match_reason}\n\n"
        f"Contract address: {new.contract_address}\n"
        f"View on DexScreener: {dex_url}\n"
        f"View on Solscan: {solscan_url}"
    )

    if new.amount_spent:
        text += f"\n\nAmount spent: {new.amount_spent:,.4f} {new.spent_symbol}"

    if new.amount:
        text += f"\nTokens bought: {new.amount:,.2f} {new.symbol or '?'}"

    # Risk level
    if token_risk and token_risk.get("level") != "UNKNOWN":
        risk_label = token_risk["level"]
        risk_reason = token_risk.get("reason", "")
        text += f"\n\nRisk: {risk_label}"
        if risk_reason:
            text += f" — {risk_reason}"

    # Wallet context
    if wallet_context:
        text += f"\n\nWallet pattern: {wallet_context}"

    # AI explanation
    if ai_explanation:
        text += f"\n\nAnalysis: {ai_explanation}"

    chat_id = _get_allowed_user_id()
    return _send_message(chat_id, text, parse_mode="")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_claim(update, context):
    """
    First-run command: claim the bot if no owner is configured.
    Once an owner is set, this command is locked out automatically.
    """
    user_id = update.effective_user.id
    current = _get_allowed_user_id()

    if current != 0:
        if user_id == current:
            await update.message.reply_text("You are already set up as the owner of this bot.", parse_mode="")
        else:
            await update.message.reply_text("This bot already has an owner configured and cannot be claimed by another account.", parse_mode="")
        return

    ok = _write_allowed_user_id(user_id)
    if ok:
        await update.message.reply_text(
            "You are now recognized as the owner of this bot. You can start managing the wallets you want to track using the add, remove, and list commands.",
            parse_mode="",
        )
        logger.info("Bot ownership claimed by Telegram user %s", user_id)
    else:
        await update.message.reply_text(
            "I could not save your ownership details. Please check the environment configuration file.",
            parse_mode="",
        )


@owner_only
async def cmd_start(update, context):
    await update.message.reply_text(
        "Hello. The wallet tracker is running and ready. You can add a wallet to watch by using add wallet followed by the address and a nickname. To stop watching one, use remove wallet followed by the nickname or address. You can also view all watched wallets by using list wallets.",
        parse_mode="",
    )


@owner_only
async def cmd_add_wallet(update, context):
    from tracker import helius as helius_api

    args = context.args
    # Smart parsing: support "/add wallet <address> <nickname>"
    if len(args) > 0 and args[0].lower() == "wallet":
        args = args[1:]

    if len(args) < 2:
        await update.message.reply_text("Please provide a wallet address and a nickname to track it, like this: add wallet address nickname.", parse_mode="")
        return

    address, nickname = args[0], " ".join(args[1:])

    if not SOLANA_ADDRESS_RE.match(address):
        await update.message.reply_text("That does not seem to be a valid Solana address. Please check the characters and try again.", parse_mode="")
        return

    res = await db_add_wallet(address, nickname, update.effective_user.id)
    if res == "limit_exceeded":
        await update.message.reply_text(
            "You are already tracking ten wallets, which is the limit. Please remove one first before adding another.",
            parse_mode=""
        )
        return
    elif res == "already_exists_address":
        await update.message.reply_text("This address is already being tracked under another name.", parse_mode="")
        return
    elif res == "already_exists_nickname":
        await update.message.reply_text(f"The nickname you chose is already in use. Please select a different nickname.", parse_mode="")
        return

    # Wrap registration in sync_to_async because it queries the DB
    register_async = sync_to_async(helius_api.register_wallet)
    ok = await register_async(address)
    status_text = "Live tracking has been activated." if ok else "I saved the wallet, but I could not start live tracking. Please check your API configuration."

    await update.message.reply_text(
        f"I am now tracking the wallet named {nickname} at address {address}. {status_text}",
        parse_mode="",
    )


@owner_only
async def cmd_remove_wallet(update, context):
    from tracker import helius as helius_api

    args = context.args
    # Smart parsing: support "/remove wallet <query>"
    if len(args) > 0 and args[0].lower() == "wallet":
        args = args[1:]

    if not args:
        await update.message.reply_text("Please specify the nickname or address of the wallet you want to remove, like this: remove wallet nickname.", parse_mode="")
        return

    query = " ".join(args)
    res = await db_remove_wallet(query)
    if not res:
        await update.message.reply_text(f"I could not find any tracked wallet matching that nickname or address.", parse_mode="")
        return

    address, nickname = res
    # Wrap unregistration in sync_to_async because it queries the DB
    unregister_async = sync_to_async(helius_api.unregister_wallet)
    ok = await unregister_async(address)
    status_text = "Live tracking has been deactivated." if ok else "I removed the wallet from the database, but I could not stop the live tracking on the server. Please check the configuration."

    await update.message.reply_text(
        f"I have stopped tracking the wallet named {nickname}. {status_text}",
        parse_mode="",
    )


@owner_only
async def cmd_list_wallets(update, context):
    from django.utils import timezone as django_tz

    wallets = await db_list_wallets()
    if not wallets:
        await update.message.reply_text("You are not tracking any wallets at the moment.")
        return

    lines = ["Here are the wallets you are currently tracking:"]
    for i, w in enumerate(wallets, 1):
        # Localize creation date
        local_date = django_tz.localtime(_make_aware(w['date_added']))
        added_str = local_date.strftime("%B %d, %Y")
        lines.append(
            f"{i}. {w['nickname']}\n"
            f"   Address: {w['address']}\n"
            f"   Tracking since: {added_str}"
        )

    body = "\n\n".join(lines)
    text = f"{body}\n\nYou are tracking {len(wallets)} out of 10 wallets."
    await update.message.reply_text(text)


@sync_to_async
def db_run_test_scenario(user_id: int, scenario_num: int) -> tuple[str, int | None]:
    from tracker.models import Wallet, TokenBuy, MatchAlert
    from tracker.matching import run_all_checks
    from django.utils import timezone
    from datetime import timedelta

    # Find or create a test wallet
    wallet = Wallet.objects.first()
    if not wallet:
        wallet = Wallet.objects.create(
            address="TestWalletAddress1111111111111111111111111",
            nickname="Test_Wallet",
            added_by_telegram_id=user_id,
        )

    # Clean old test buys for this test wallet to keep database clean
    TokenBuy.objects.filter(wallet=wallet).delete()

    now = timezone.now()

    if scenario_num == 1:
        # Scenario 1: The Black Bull (identical name, different logo)
        past_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="The Black Bull",
            symbol="BULL",
            logo_url="https://example.com/logo1.png",
            logo_hash="0000000000000000",
            contract_address="PastContractAddress11111111111111111111",
            timestamp=now - timedelta(days=6),
            amount=100.0,
            amount_spent=1.2,
            spent_symbol="SOL",
            tx_signature="mock_sig_past_1",
        )
        new_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="The Black Bull",
            symbol="BULL",
            logo_url="https://example.com/logo2.png",
            logo_hash="ffffffffffffffff",  # different logo
            contract_address="NewContractAddress111111111111111111111",
            timestamp=now,
            amount=150.0,
            amount_spent=1.8,
            spent_symbol="SOL",
            tx_signature="mock_sig_new_1",
        )
        msg = "🧪 Scenario 1: Identical name match ('The Black Bull')"
    elif scenario_num == 2:
        # Scenario 2: Dumacrats (identical name, different logo)
        past_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="Dumacrats",
            symbol="DUMA",
            logo_url="https://example.com/logo1.png",
            logo_hash="0000000000000000",
            contract_address="PastContractAddress11111111111111111111",
            timestamp=now - timedelta(days=4),
            amount=50.0,
            amount_spent=50.0,
            spent_symbol="USDC",
            tx_signature="mock_sig_past_2",
        )
        new_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="Dumacrats",
            symbol="DUMA",
            logo_url="https://example.com/logo2.png",
            logo_hash="ffffffffffffffff",  # different logo
            contract_address="NewContractAddress111111111111111111111",
            timestamp=now,
            amount=75.0,
            amount_spent=75.0,
            spent_symbol="USDC",
            tx_signature="mock_sig_new_2",
        )
        msg = "🧪 Scenario 2: Identical name match ('Dumacrats')"
    else:
        # Scenario 3: The White Whale vs The White Whale V2 (near-identical name)
        past_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="The White Whale",
            symbol="WHALE",
            logo_url="https://example.com/whale.png",
            logo_hash="a1a1a1a1a1a1a1a1",
            contract_address="PastContractAddress11111111111111111111",
            timestamp=now - timedelta(days=3),
            amount=500.0,
            amount_spent=2.5,
            spent_symbol="SOL",
            tx_signature="mock_sig_past_3",
        )
        new_buy = TokenBuy.objects.create(
            wallet=wallet,
            name="The White Whale V2",
            symbol="WHALE2",
            logo_url="https://example.com/whale2.png",
            logo_hash="a1a1a1a1a1a1a1a1",  # similar logo
            contract_address="NewContractAddress111111111111111111111",
            timestamp=now,
            amount=600.0,
            amount_spent=3.0,
            spent_symbol="SOL",
            tx_signature="mock_sig_new_3",
        )
        msg = "🧪 Scenario 3: Near-identical fuzzy match ('The White Whale' vs 'The White Whale V2')"

    # Run check logic
    past_buys = TokenBuy.objects.filter(wallet=wallet).exclude(pk=new_buy.pk)
    matches = run_all_checks(new_buy, past_buys)

    alert_id = None
    if matches:
        match_result = matches[0]
        alert = MatchAlert.objects.create(
            new_buy=new_buy,
            matched_buy=past_buy,
            match_type=match_result.match_type,
            name_score=match_result.name_score,
            symbol_score=match_result.symbol_score,
            logo_distance=match_result.logo_distance,
        )
        alert_id = alert.pk

    return msg, alert_id


@owner_only
async def cmd_test(update, context):
    """
    Test command to simulate matching scenarios.
    Usage: /test <1, 2, or 3>
    """
    from tracker.models import MatchAlert

    args = context.args
    scenario = 3  # default to White Whale V2
    if args:
        try:
            scenario = int(args[0])
            if scenario not in [1, 2, 3]:
                scenario = 3
        except ValueError:
            pass

    status_msg = await update.message.reply_text("Running the matching simulator now.", parse_mode="")
    msg, alert_id = await db_run_test_scenario(update.effective_user.id, scenario)

    if alert_id:
        # Load and fire the alert message
        @sync_to_async
        def get_alert(pk):
            return MatchAlert.objects.select_related('new_buy', 'matched_buy', 'new_buy__wallet').get(pk=pk)

        alert = await get_alert(alert_id)
        
        # Calculate time diff
        time_diff = format_time_diff(alert.new_buy.timestamp, alert.matched_buy.timestamp)
        
        # Compute match reason
        match_parts = []
        if alert.name_score is not None and alert.name_score >= settings.NAME_MATCH_THRESHOLD:
            match_parts.append(f"similar name ({alert.name_score:.0f}%)")
        if alert.symbol_score is not None and alert.symbol_score >= settings.SYMBOL_MATCH_THRESHOLD:
            match_parts.append(f"similar symbol ({alert.symbol_score:.0f}%)")
        if alert.logo_distance is not None and alert.logo_distance <= settings.LOGO_MATCH_THRESHOLD:
            match_parts.append("similar logo")
        match_reason = " and ".join(match_parts) if match_parts else alert.match_type

        # Fetch AI metrics asynchronously
        @sync_to_async
        def fetch_ai_fields(alert, time_diff, match_reason):
            from tracker.ai import get_token_risk, get_wallet_context, get_ai_explanation
            from tracker.models import TokenBuy
            
            token_risk = get_token_risk(
                name=alert.new_buy.name or "Unknown",
                symbol=alert.new_buy.symbol or "?",
                contract_address=alert.new_buy.contract_address,
            )
            
            past_buys = TokenBuy.objects.filter(wallet=alert.new_buy.wallet).exclude(pk=alert.new_buy.pk)
            recent_buys_data = [
                {
                    "name": b.name or "?",
                    "symbol": b.symbol or "?",
                    "timestamp_str": b.timestamp.strftime("%b %d"),
                }
                for b in past_buys.order_by("-timestamp")[:15]
            ]
            wallet_context = get_wallet_context(
                wallet_nickname=alert.new_buy.wallet.nickname,
                recent_buys=recent_buys_data,
            )
            
            ai_explanation = get_ai_explanation(
                new_name=alert.new_buy.name or "Unknown",
                new_symbol=alert.new_buy.symbol or "?",
                past_name=alert.matched_buy.name or "Unknown",
                past_symbol=alert.matched_buy.symbol or "?",
                time_diff=time_diff,
                match_reason=match_reason,
                wallet_nickname=alert.new_buy.wallet.nickname,
            )
            return token_risk, wallet_context, ai_explanation

        token_risk, wallet_context, ai_explanation = await fetch_ai_fields(alert, time_diff, match_reason)

        send_alert(
            alert,
            ai_explanation=ai_explanation,
            token_risk=token_risk,
            wallet_context=wallet_context,
        )
        await status_msg.edit_text(f"Simulating scenario: {msg}. The similarity alert was triggered successfully.", parse_mode="")
    else:
        await status_msg.edit_text(f"Simulating scenario: {msg}. No similarity match was found under the current settings.", parse_mode="")


@owner_only
async def cmd_clear(update, context):
    """Deletes up to the last 100 messages in the chat to clear the screen."""
    chat_id = update.effective_chat.id
    current_id = update.message.message_id
    
    # Send a temporary status message
    status_msg = await update.message.reply_text("Clearing the recent messages from this chat now.", parse_mode="")
    
    deleted_count = 0
    failed_count = 0
    
    # Attempt to delete recent messages backwards from the current message
    import asyncio
    import telegram.error

    for msg_id in range(current_id, current_id - 100, -1):
        if msg_id == status_msg.message_id:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted_count += 1
            await asyncio.sleep(0.05)  # small delay to prevent Telegram rate limit bans
        except telegram.error.RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                deleted_count += 1
            except Exception:
                failed_count += 1
        except Exception:
            failed_count += 1
            
    # Report back successes and failures
    try:
        explanation = ""
        if failed_count > 0:
            explanation = f" I could not delete {failed_count} of them because they are likely older than forty-eight hours."
            
        await status_msg.edit_text(
            f"I have successfully cleared {deleted_count} messages from this conversation.{explanation}",
            parse_mode=""
        )
        import asyncio
        await asyncio.sleep(4)  # Give the user a bit more time to read the summary
        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        pass


async def error_handler(update, context):
    """Global safety net: inform the user of unexpected errors instead of failing silently."""
    logger.error("Exception while handling an update: %s", context.error, exc_info=context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "An unexpected error occurred while processing your request. Please try again later.",
                parse_mode=""
            )
        except Exception:
            pass


@owner_only
async def cmd_profile(update, context):
    """
    /profile <nickname> — show AI-generated profile for a tracked wallet.
    """
    from tracker.models import Wallet, TokenBuy, MatchAlert
    from tracker.ai import generate_wallet_profile

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /profile <wallet nickname>\n"
            "Example: /profile shamo",
            parse_mode=""
        )
        return

    nickname = " ".join(args).strip()
    try:
        wallet = await sync_to_async(Wallet.objects.get)(nickname__iexact=nickname)
    except Wallet.DoesNotExist:
        await update.message.reply_text(
            f"No wallet found with the nickname '{nickname}'.\n"
            f"Use /list to see tracked wallets.",
            parse_mode=""
        )
        return

    await update.message.reply_text(f"Generating profile for {wallet.nickname}...")

    buys = await sync_to_async(list)(
        TokenBuy.objects.filter(wallet=wallet).order_by("-timestamp")[:30]
    )
    alert_count = await sync_to_async(
        MatchAlert.objects.filter(new_buy__wallet=wallet).count
    )()

    buy_history = [
        {
            "name": b.name or "?",
            "symbol": b.symbol or "?",
            "timestamp_str": b.timestamp.strftime("%b %d"),
        }
        for b in buys
    ]

    profile = await sync_to_async(generate_wallet_profile)(
        wallet_nickname=wallet.nickname,
        buy_history=buy_history,
        alert_count=alert_count,
    )

    if not profile:
        await update.message.reply_text(
            f"Could not generate a profile for {wallet.nickname} right now. Try again shortly.",
            parse_mode=""
        )
        return

    total_buys = len(buys)
    await update.message.reply_text(
        f"Profile: {wallet.nickname}\n"
        f"Address: {wallet.address[:12]}...\n"
        f"Total buys recorded: {total_buys}\n"
        f"Total alerts triggered: {alert_count}\n\n"
        f"{profile}",
        parse_mode=""
    )


@owner_only
async def cmd_natural_language(update, context):
    """
    Catch-all handler for plain English messages.
    The AI interprets what the user wants and either replies or triggers an action.
    """
    from tracker.models import Wallet, TokenBuy, MatchAlert
    from tracker.ai import understand_message
    from django.utils import timezone as django_tz
    from datetime import timedelta

    user_text = update.message.text or update.message.caption or ""
    if not user_text.strip():
        fail_text = "I can only understand text messages, questions, and wallet commands. Please send me a text message or use the menu commands."
        await update.message.reply_text(fail_text, parse_mode="")
        return

    # Check for pending action BEFORE doing anything else
    pending = context.user_data.get("pending_action")
    if pending:
        text_stripped = user_text.strip().lower()
        if text_stripped == "yes":
            action = pending.get("action")
            context.user_data.pop("pending_action", None)
            
            if "nl_history" not in context.user_data:
                context.user_data["nl_history"] = []
            history = context.user_data["nl_history"]
            history.append({"role": "user", "content": user_text})
            
            if action == "add_wallet":
                address = pending.get("address", "")
                nickname = pending.get("nickname", "")
                context.args = [address, nickname]
                await cmd_add_wallet(update, context)
                history.append({"role": "assistant", "content": f"I have added the wallet named {nickname} with address {address} to the watch list."})
            elif action == "remove_wallet":
                nickname = pending.get("nickname", "")
                context.args = [nickname]
                await cmd_remove_wallet(update, context)
                history.append({"role": "assistant", "content": f"I have removed the wallet named {nickname} from the watch list."})
            
            if len(history) > 10:
                context.user_data["nl_history"] = history[-10:]
            return

        elif text_stripped == "no":
            context.user_data.pop("pending_action", None)
            
            if "nl_history" not in context.user_data:
                context.user_data["nl_history"] = []
            history = context.user_data["nl_history"]
            history.append({"role": "user", "content": user_text})
            
            reply_text = "Okay, cancelled."
            await update.message.reply_text(reply_text, parse_mode="")
            history.append({"role": "assistant", "content": reply_text})
            
            if len(history) > 10:
                context.user_data["nl_history"] = history[-10:]
            return

        else:
            action = pending.get("action")
            if action == "add_wallet":
                nickname = pending.get("nickname", "")
                address = pending.get("address", "")
                msg = f"I understood: add the wallet named {nickname} with address {address}. Reply yes to confirm, or no to cancel."
            elif action == "remove_wallet":
                nickname = pending.get("nickname", "")
                msg = f"I understood: remove the wallet named {nickname}. Reply yes to confirm, or no to cancel."
            else:
                msg = "Please confirm or cancel the pending action first. Reply yes to confirm, or no to cancel."
            await update.message.reply_text(msg, parse_mode="")
            return

    # Initialize history list if it doesn't exist
    if "nl_history" not in context.user_data:
        context.user_data["nl_history"] = []
    
    history = context.user_data["nl_history"]

    # Build context for the AI
    wallet_names = await sync_to_async(list)(
        Wallet.objects.values_list("nickname", flat=True)
    )
    total_buys = await sync_to_async(TokenBuy.objects.count)()
    since = django_tz.now() - timedelta(hours=24)
    alerts_today = await sync_to_async(
        MatchAlert.objects.filter(sent_at__gte=since).count
    )()

    await update.message.reply_text("Thinking...")

    result = await sync_to_async(understand_message)(
        user_text=user_text,
        history=history,
        wallet_names=list(wallet_names),
        total_buys=total_buys,
        alerts_today=alerts_today,
    )

    # Append user's input to history
    history.append({"role": "user", "content": user_text})

    action_type = result.get("type")

    if action_type == "reply":
        reply_text = result.get("text", "I could not understand that.")
        await update.message.reply_text(reply_text, parse_mode="")
        history.append({"role": "assistant", "content": reply_text})

    elif action_type == "action":
        action = result.get("action", "")

        if action == "list_wallets":
            await cmd_list_wallets(update, context)
            history.append({"role": "assistant", "content": "I have listed the tracked wallets."})

        elif action == "add_wallet":
            address = result.get("address", "")
            nickname = result.get("nickname", "")
            if address and nickname:
                context.user_data["pending_action"] = {
                    "action": "add_wallet",
                    "address": address,
                    "nickname": nickname
                }
                msg = f"I understood: add the wallet named {nickname} with address {address}. Reply yes to confirm, or no to cancel."
                await update.message.reply_text(msg, parse_mode="")
                history.append({"role": "assistant", "content": msg})
            else:
                fail_text = (
                    "I understood you want to add a wallet but could not extract the address or nickname. "
                    "Please tell me the Solana address and what nickname you want to give it."
                )
                await update.message.reply_text(fail_text, parse_mode="")
                history.append({"role": "assistant", "content": fail_text})

        elif action == "remove_wallet":
            nickname = result.get("nickname", "")
            if nickname:
                context.user_data["pending_action"] = {
                    "action": "remove_wallet",
                    "nickname": nickname
                }
                msg = f"I understood: remove the wallet named {nickname}. Reply yes to confirm, or no to cancel."
                await update.message.reply_text(msg, parse_mode="")
                history.append({"role": "assistant", "content": msg})
            else:
                fail_text = "Which wallet would you like to remove? Please provide its nickname."
                await update.message.reply_text(fail_text, parse_mode="")
                history.append({"role": "assistant", "content": fail_text})

        elif action == "profile":
            nickname = result.get("nickname", "")
            if nickname:
                context.args = [nickname]
                await cmd_profile(update, context)
                history.append({"role": "assistant", "content": f"I have displayed the profile for {nickname}."})
            else:
                fail_text = "Which wallet would you like a profile for? Please provide its nickname."
                await update.message.reply_text(fail_text, parse_mode="")
                history.append({"role": "assistant", "content": fail_text})

        else:
            fail_text = f"I understood your intent but don't know how to do '{action}' yet."
            await update.message.reply_text(fail_text, parse_mode="")
            history.append({"role": "assistant", "content": fail_text})

    else:
        fail_text = "I did not understand that. Try typing a command or ask me a question."
        await update.message.reply_text(fail_text, parse_mode="")
        history.append({"role": "assistant", "content": fail_text})

    # Keep history capped at 10 items to prevent context growth
    if len(history) > 10:
        context.user_data["nl_history"] = history[-10:]



# ── Bot runner ────────────────────────────────────────────────────────────────

async def post_init(application):
    """Register commands menu with Telegram on bot startup."""
    from telegram import BotCommand
    
    owner_id = _get_allowed_user_id()
    
    commands = [
        BotCommand("start", "Welcome & overview of tracking capabilities"),
    ]
    
    if owner_id == 0:
        commands.append(BotCommand("claim", "First-run: Claim bot ownership"))
        
    commands.extend([
        BotCommand("add", "Track a new Solana wallet"),
        BotCommand("remove", "Stop tracking a wallet"),
        BotCommand("list", "Display monitored wallets and statuses"),
        BotCommand("profile", "Generate AI personality & strategy profile"),
        BotCommand("clear", "Clear the conversation history from chat"),
        BotCommand("test", "Simulate a buy alert & AI analysis")
    ])
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered with Telegram.")


def build_application():
    """Build and return the python-telegram-bot Application."""
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, PicklePersistence

    persistence_path = os.path.join(settings.BASE_DIR, "bot_persistence.pickle")
    persistence = PicklePersistence(filepath=persistence_path)

    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("claim", cmd_claim))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("profile", cmd_profile))

    # Register handlers for both specific and generic commands
    app.add_handler(CommandHandler("addwallet", cmd_add_wallet))
    app.add_handler(CommandHandler("add", cmd_add_wallet))

    app.add_handler(CommandHandler("removewallet", cmd_remove_wallet))
    app.add_handler(CommandHandler("remove", cmd_remove_wallet))

    app.add_handler(CommandHandler("listwallets", cmd_list_wallets))
    app.add_handler(CommandHandler("list", cmd_list_wallets))

    # Natural language catch-all (must be last)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, cmd_natural_language))

    return app
