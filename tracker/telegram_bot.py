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


def _send_message(chat_id: int | str, text: str, parse_mode: str = "HTML", reply_markup: dict | None = None, link_preview_options: dict | None = None) -> bool:
    """Send a message via the Telegram Bot API HTTP endpoint."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if link_preview_options:
        payload["link_preview_options"] = link_preview_options
    try:
        r = requests.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", exc)
        return False


def _send_photo(chat_id: int | str, photo_url: str, caption: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    """Send a photo via the Telegram Bot API HTTP endpoint."""
    if len(caption) > 1024:
        logger.warning("Caption length %d exceeds Telegram limit of 1024. Falling back to sendMessage.", len(caption))
        return False

    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"{TELEGRAM_API_BASE}/sendPhoto",
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Telegram sendPhoto failed: %s", exc)
        return False


def _get_allowed_user_ids() -> list[int]:
    """
    Return all allowed user/chat IDs from the .env or settings.
    Re-reads from environment each call so changes written to .env
    by /claim are picked up without restarting the bot.
    """
    env_path = Path(settings.BASE_DIR) / ".env"
    raw_val = None
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_ALLOWED_USER_IDS="):
                raw_val = line.split("=", 1)[1].strip()
                break
            elif line.startswith("TELEGRAM_ALLOWED_USER_ID="):
                raw_val = line.split("=", 1)[1].strip()

    if raw_val is None:
        return settings.TELEGRAM_ALLOWED_USER_IDS

    user_ids = []
    for item in raw_val.split(","):
        try:
            val = int(item.strip())
            if val != 0:
                user_ids.append(val)
        except ValueError:
            pass
    return user_ids or [0]


def _get_allowed_user_id() -> int:
    """
    Return the primary allowed user ID (the first one in the list).
    Kept for backwards compatibility with setup scripts and ownership checks.
    """
    ids = _get_allowed_user_ids()
    return ids[0] if ids else 0


def _write_allowed_user_id(user_id: int) -> bool:
    """Patch TELEGRAM_ALLOWED_USER_IDS in the .env file at runtime."""
    env_path = Path(settings.BASE_DIR) / ".env"
    if not env_path.exists():
        return False
    content = env_path.read_text()
    if "TELEGRAM_ALLOWED_USER_IDS=" in content:
        new_content = re.sub(
            r"^TELEGRAM_ALLOWED_USER_IDS=.*$",
            f"TELEGRAM_ALLOWED_USER_IDS={user_id}",
            content,
            flags=re.MULTILINE,
        )
    else:
        new_content = re.sub(
            r"^TELEGRAM_ALLOWED_USER_ID=.*$",
            f"TELEGRAM_ALLOWED_USER_IDS={user_id}",
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
        allowed_ids = _get_allowed_user_ids()

        if not allowed_ids or allowed_ids == [0]:
            await update.message.reply_text(
                "No owner configured yet. Send /claim to claim this bot as yours.",
                parse_mode="",
            )
            return

        if user_id not in allowed_ids:
            await update.message.reply_text(
                "You are not authorized to use this bot. Please contact the administrator to grant access for your account.",
                parse_mode="",
            )
            return

        return await handler(update, context)
    return wrapper


def format_compact_usd(val: float | Decimal) -> str:
    """Format large numbers into compact K/M/B notation."""
    try:
        val_float = float(val)
    except (ValueError, TypeError):
        return str(val)

    if val_float >= 1_000_000_000:
        return f"${val_float / 1_000_000_000:.1f}B"
    if val_float >= 1_000_000:
        return f"${val_float / 1_000_000:.1f}M"
    if val_float >= 1_000:
        return f"${val_float / 1_000:.1f}K"
    return f"${val_float:.0f}"


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


def send_alert(alert, token_risk: dict | None = None, dev_link_text: str = "", security_info: dict | None = None, **kwargs) -> bool:
    from django.utils import timezone as django_tz

    new = alert.new_buy
    past = alert.matched_buy
    wallet = new.wallet

    # Format localized timestamps
    local_new = django_tz.localtime(_make_aware(new.timestamp))
    local_past = django_tz.localtime(_make_aware(past.timestamp))
    new_time = local_new.strftime("%b %d, %Y at %I:%M %p")
    past_time = local_past.strftime("%b %d, %Y at %I:%M %p")

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

    # Format market cap using compact USD helper if available
    mc_text = ""
    if token_risk and "dex_data" in token_risk:
        mc = token_risk["dex_data"].get("market_cap")
        if mc:
            mc_text = f"\n📊 <b>Market Cap:</b> {format_compact_usd(mc)}"

    # Format risk level with color-coded emoji
    risk_text = ""
    if token_risk and token_risk.get("level") != "UNKNOWN":
        level = token_risk["level"]
        risk_emoji = "🔴" if level == "HIGH" else ("🟡" if level == "MEDIUM" else "🟢")
        reason = token_risk.get("reason", "")
        risk_text = f"\n\n⚡️ <b>Risk Level:</b> {risk_emoji} <b>{level}</b>"
        if reason:
            risk_text += f"\n└ <i>{reason}</i>"

    # Format security and holder distribution info
    security_checks_text = ""
    holders_text = ""
    if security_info:
        # 1. Holders
        dist = security_info.get("holders_dist")
        if dist:
            top_10 = dist.get("top_10_percent", 0.0)
            top_20 = dist.get("top_20_percent", 0.0)
            total_supply = dist.get("total_supply", 0.0)
            
            creator_bal = security_info.get("creator_balance", 0.0)
            creator_pct = (creator_bal / total_supply) * 100.0 if total_supply > 0 else 0.0
            
            if creator_bal > 0:
                dev_bal_text = f"{creator_pct:.1f}% ({format_compact_usd(creator_bal).replace('$', '')} tokens)"
            else:
                dev_bal_text = "0% (0 tokens)"

            top_10_warn = ""
            if top_10 >= settings.HOLDER_TOP10_WARN_THRESHOLD:
                top_10_warn = " ⚠️ (High concentration)"
            
            dev_warn = ""
            if creator_pct >= settings.DEV_HOLDING_WARN_THRESHOLD:
                dev_warn = " ⚠️ (High dev holding)"

            holders_text = (
                f"\n\n👥 <b>Holder Distribution:</b>\n"
                f"├ <b>Top 10 hold:</b> {top_10:.1f}%{top_10_warn}\n"
                f"├ <b>Top 20 hold:</b> {top_20:.1f}%\n"
                f"└ <b>Developer:</b> {dev_bal_text}{dev_warn}"
            )
            
        # 2. Mint Security
        mint_sec = security_info.get("mint_security")
        if mint_sec:
            mint_auth = mint_sec.get("mint_authority")
            freeze_auth = mint_sec.get("freeze_authority")
            
            mint_status = "✅ Revoked (Cannot mint)" if mint_auth is None else "⚠️ Enabled (Dev can mint!)"
            freeze_status = "✅ Revoked (Cannot freeze)" if freeze_auth is None else "⚠️ Enabled (Honeypot risk!)"
            
            security_checks_text = (
                f"\n\n🔒 <b>Security Checks:</b>\n"
                f"├ <b>Mint Authority:</b> {mint_status}\n"
                f"└ <b>Freeze Authority:</b> {freeze_status}"
            )

    # Prepend hidden link to DexScreener page instead of raw WebP logo.
    # Webpages support prefer_small_media natively for clean sidebar layout.
    hidden_logo_prefix = f'<a href="{dex_url}">&#8203;</a>'
    link_preview_opts = {
        "url": dex_url,
        "prefer_small_media": True,
        "show_above_text": False
    }

    # Build rich HTML message
    text = (
        f"{hidden_logo_prefix}🚨 <b>Similarity Alert for {wallet.nickname}</b>\n\n"
        f"🆕 <b>New Buy:</b> <b>{new.name or '?'}</b> ({new.symbol or '?'})\n"
        f"⏰ <b>Bought:</b> {new_time}\n"
        f"💳 <b>Spent:</b> <code>{new.amount_spent:,.4f}</code> {new.spent_symbol} "
    )
    if new.amount:
        text += f"(obtained <code>{new.amount:,.2f}</code> {new.symbol or '?'})"
    
    text += (
        f"{mc_text}\n\n"
        f"🔄 <b>Matched Buy:</b> <b>{past.name or '?'}</b> ({past.symbol or '?'})\n"
        f"⏰ <b>Bought:</b> {past_time}\n"
        f"⏳ <b>Time Between:</b> {time_diff}\n"
        f"🎯 <b>Match Reason:</b> {match_reason}\n\n"
        f"🔑 <b>Contract:</b> <code>{new.contract_address}</code>"
        f"{risk_text}"
        f"{holders_text}"
        f"{security_checks_text}"
        f"{dev_link_text}"
    )

    # Construct Inline Keyboard buttons
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📈 DexScreener", "url": dex_url},
                {"text": "🔍 Solscan", "url": solscan_url},
            ],
            [
                {"text": "👤 Wallet Profile", "callback_data": f"profile_{wallet.nickname}"},
                {"text": "❌ Stop Tracking", "callback_data": f"remove_{wallet.nickname}"},
            ]
        ]
    }

    chat_ids = _get_allowed_user_ids()
    if not chat_ids:
        logger.warning("No allowed Telegram chat IDs configured to receive alerts.")
        return False

    success = True
    logo_url = new.logo_url
    for chat_id in chat_ids:
        ok = False
        if logo_url:
            ok = _send_photo(
                chat_id=chat_id,
                photo_url=logo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            if not ok:
                logger.info("sendPhoto failed or caption too long; falling back to sendMessage for chat_id %s", chat_id)
        
        if not ok:
            ok = _send_message(
                chat_id, 
                text, 
                parse_mode="HTML", 
                reply_markup=reply_markup,
                link_preview_options=link_preview_opts
            )
        if not ok:
            success = False
    return success


def send_coordinated_alert(contract_address: str, buys: list, token_risk: dict | None = None) -> bool:
    """Send a coordinated buy alert showing all participating wallets."""
    from django.utils import timezone as django_tz
    from tracker.telegram_bot import _send_message, _get_allowed_user_ids, format_compact_usd

    if not buys:
        return False

    first_buy = buys[0]
    wallet_count = len(buys)
    token_name = first_buy.name or "Unknown"
    token_symbol = first_buy.symbol or "?"

    # Format list of buyers
    buyer_lines = []
    now = django_tz.now()
    for i, tb in enumerate(buys, start=1):
        diff = now - tb.timestamp
        mins_ago = int(diff.total_seconds() // 60)
        time_str = "just now" if mins_ago < 1 else f"{mins_ago}m ago"
        buyer_lines.append(
            f"{i}. 👤 <b>{tb.wallet.nickname}</b> ({time_str}): <code>{tb.amount_spent:,.4f}</code> {tb.spent_symbol}"
        )
    buyers_text = "\n".join(buyer_lines)

    dex_url = f"https://dexscreener.com/solana/{contract_address}"
    solscan_url = f"https://solscan.io/token/{contract_address}"

    # Format market cap if available
    mc_text = ""
    if token_risk and "dex_data" in token_risk:
        mc = token_risk["dex_data"].get("market_cap")
        if mc:
            mc_text = f"\n📊 <b>Market Cap:</b> {format_compact_usd(mc)}"

    # Format risk level
    risk_text = ""
    if token_risk and token_risk.get("level") != "UNKNOWN":
        level = token_risk["level"]
        risk_emoji = "🔴" if level == "HIGH" else ("🟡" if level == "MEDIUM" else "🟢")
        reason = token_risk.get("reason", "")
        risk_text = f"\n\n⚡️ <b>Risk Level:</b> {risk_emoji} <b>{level}</b>"
        if reason:
            risk_text += f"\n└ <i>{reason}</i>"

    # Prepend hidden link to DexScreener page for clean sidebar layout
    hidden_logo_prefix = f'<a href="{dex_url}">&#8203;</a>'
    link_preview_opts = {
        "url": dex_url,
        "prefer_small_media": True,
        "show_above_text": False
    }

    text = (
        f"{hidden_logo_prefix}🚨 <b>Coordinated Buy Alert! ({wallet_count} Wallets)</b>\n\n"
        f"👥 <b>{wallet_count} wallets</b> accumulated <b>{token_name} ({token_symbol})</b> in the last 60 minutes:\n"
        f"{buyers_text}"
        f"{mc_text}\n\n"
        f"🔑 <b>Contract:</b> <code>{contract_address}</code>"
        f"{risk_text}"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📈 DexScreener", "url": dex_url},
                {"text": "🔍 Solscan", "url": solscan_url},
            ]
        ]
    }

    chat_ids = _get_allowed_user_ids()
    if not chat_ids:
        logger.warning("No allowed Telegram chat IDs configured to receive coordinated alerts.")
        return False

    success = True
    logo_url = first_buy.logo_url
    for chat_id in chat_ids:
        ok = False
        if logo_url:
            ok = _send_photo(
                chat_id=chat_id,
                photo_url=logo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            if not ok:
                logger.info("sendPhoto failed or caption too long; falling back to sendMessage for coordinated alert, chat_id %s", chat_id)
        
        if not ok:
            ok = _send_message(
                chat_id, 
                text, 
                parse_mode="HTML", 
                reply_markup=reply_markup,
                link_preview_options=link_preview_opts
            )
        if not ok:
            success = False
    return success



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
    welcome_text = (
        "🤖 <b>Solana Wallet-Tracking Bot</b>\n\n"
        "Hello! I am active and monitoring the Solana blockchain. I will notify you "
        "instantly whenever your monitored wallets buy repeating tokens.\n\n"
        "📝 <b>Quick Commands:</b>\n"
        "• <b>Add wallet:</b> <code>add [address] [nickname]</code>\n"
        "• <b>Remove wallet:</b> <code>remove [nickname/address]</code>\n"
        "• <b>List wallets:</b> <code>/list</code>\n"
        "• <b>Wallet Profile:</b> <code>/profile [nickname]</code>\n"
        "• <b>Clear screen:</b> <code>/clear</code>\n\n"
        "Use the menu button or click the options below to get started!"
    )
    
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📋 List Monitored Wallets", "callback_data": "list_wallets_cmd"},
            ],
            [
                {"text": "➕ Add Wallet Help", "callback_data": "add_wallet_help"},
            ]
        ]
    }
    
    await update.message.reply_text(
        welcome_text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )


async def handle_callback_query(update, context):
    """Handle click events on inline keyboard buttons."""
    query = update.callback_query
    await query.answer()

    # Route update.message to the callback query's message so decorators/handlers work
    if update.message is None and query.message is not None:
        if hasattr(update, "_unfreeze"):
            update._unfreeze()
        update.message = query.message
        if hasattr(update, "_freeze"):
            update._freeze()

    data = query.data
    user_id = update.effective_user.id
    allowed = _get_allowed_user_id()

    # Enforce access control on callback queries
    if allowed != 0 and user_id != allowed:
        await query.message.reply_text(
            "You are not authorized to use this bot.",
            parse_mode=""
        )
        return

    if data.startswith("profile_"):
        nickname = data.split("_", 1)[1]
        context.args = [nickname]
        await cmd_profile(update, context)
        
    elif data.startswith("remove_"):
        nickname = data.split("_", 1)[1]
        context.user_data["pending_action"] = {
            "action": "remove_wallet",
            "nickname": nickname
        }
        await query.message.reply_text(
            f"⚠️ You clicked Stop Tracking. Do you want to remove the wallet named {nickname}? Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )
        
    elif data == "list_wallets_cmd":
        await cmd_list_wallets(update, context)
        
    elif data == "add_wallet_help":
        await query.message.reply_text(
            "💡 <b>To track a new wallet:</b>\n"
            "Simply send the Solana address and a nickname. For example:\n"
            "<code>add 6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY trader_shamo</code>",
            parse_mode="HTML"
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

    # Trigger Celery backfill task asynchronously (runs in background to not block Telegram)
    from tracker.tasks import backfill_wallet_history_task
    backfill_wallet_history_task.delay(address, nickname, update.effective_user.id)


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

    total_buys = len(buys)
    if not profile:
        profile_text = (
            f"📊 <b>Wallet Activity Report</b>\n"
            f"• <b>Monitored since:</b> {wallet.date_added.strftime('%B %d, %Y')}\n"
            f"• <b>Total swaps recorded:</b> <code>{total_buys}</code>\n"
            f"• <b>Total similarity alerts:</b> <code>{alert_count}</code>\n\n"
            f"📝 <b>Recent Purchases:</b>\n"
        )
        if buy_history:
            for b in buy_history[:5]:
                profile_text += f"• {b['name']} ({b['symbol']}) — {b['timestamp_str']}\n"
        else:
            profile_text += "• No buys recorded yet."
            
        await update.message.reply_text(
            f"👤 <b>Profile:</b> {wallet.nickname}\n"
            f"🔑 <b>Address:</b> <code>{wallet.address}</code>\n\n"
            f"{profile_text}",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(
        f"👤 <b>Profile:</b> {wallet.nickname}\n"
        f"🔑 <b>Address:</b> <code>{wallet.address}</code>\n"
        f"• <b>Total buys recorded:</b> <code>{total_buys}</code>\n"
        f"• <b>Total alerts triggered:</b> <code>{alert_count}</code>\n\n"
        f"{profile}",
        parse_mode="HTML"
    )


@owner_only
async def cmd_natural_language(update, context):
    """
    Catch-all handler for plain English messages.
    Deterministic rule-based parser that handles commands, confirmation flow,
    and multi-step adding of wallets without relying on external AI API.
    """
    user_text = update.message.text or update.message.caption or ""
    if not user_text.strip():
        fail_text = "I can only understand text messages, questions, and wallet commands. Please send me a text message or use the menu commands."
        await update.message.reply_text(fail_text, parse_mode="")
        return

    # Check for pending action (Confirmation gate: yes/no)
    pending = context.user_data.get("pending_action")
    if pending:
        text_stripped = user_text.strip().lower()
        if text_stripped == "yes":
            action = pending.get("action")
            context.user_data.pop("pending_action", None)
            
            if action == "add_wallet":
                address = pending.get("address", "")
                nickname = pending.get("nickname", "")
                context.args = [address, nickname]
                await cmd_add_wallet(update, context)
            elif action == "remove_wallet":
                nickname = pending.get("nickname", "")
                context.args = [nickname]
                await cmd_remove_wallet(update, context)
            return

        elif text_stripped == "no":
            context.user_data.pop("pending_action", None)
            await update.message.reply_text("Okay, cancelled.", parse_mode="")
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

    # Check for pending add address (Multi-step add wallet flow)
    pending_add_address = context.user_data.get("pending_add_address")
    if pending_add_address:
        nickname = user_text.strip()
        # Verify the nickname is not empty or a Solana address itself
        if nickname and not SOLANA_ADDRESS_RE.match(nickname):
            context.user_data.pop("pending_add_address", None)
            context.user_data["pending_action"] = {
                "action": "add_wallet",
                "address": pending_add_address,
                "nickname": nickname
            }
            msg = f"I understood: add the wallet named {nickname} with address {pending_add_address}. Reply yes to confirm, or no to cancel."
            await update.message.reply_text(msg, parse_mode="")
            return
        # If it is a Solana address, treat it as updating the pending address instead
        elif SOLANA_ADDRESS_RE.match(nickname):
            context.user_data["pending_add_address"] = nickname
            await update.message.reply_text(
                f"Great! I have the address. What nickname would you like to assign to this wallet?",
                parse_mode=""
            )
            return

    # Normalize user input for matching
    cleaned_text = user_text.strip()
    lower_text = cleaned_text.lower()
    
    # 1. List Wallets
    list_patterns = ["list", "lists", "listwallets", "listwallet", "showwallets", "showwallet"]
    if lower_text in list_patterns or lower_text == "list wallets":
        await cmd_list_wallets(update, context)
        return

    # 2. Add Wallet
    # Support "add wallet <address> <nickname>", "add <address> <nickname>"
    # Or just pasting the address.
    if lower_text.startswith("add") or SOLANA_ADDRESS_RE.search(cleaned_text):
        # Extract Solana address
        words = cleaned_text.split()
        address = None
        for w in words:
            if SOLANA_ADDRESS_RE.match(w):
                address = w
                break
        
        if address:
            # Reconstruct nickname from remaining words
            nickname_words = [w for w in words if w != address and w.lower() not in ["add", "wallet"]]
            nickname = " ".join(nickname_words).strip()
            
            if nickname:
                context.user_data["pending_action"] = {
                    "action": "add_wallet",
                    "address": address,
                    "nickname": nickname
                }
                msg = f"I understood: add the wallet named {nickname} with address {address}. Reply yes to confirm, or no to cancel."
                await update.message.reply_text(msg, parse_mode="")
            else:
                context.user_data["pending_add_address"] = address
                await update.message.reply_text(
                    f"Great! I have the address. What nickname would you like to assign to this wallet?",
                    parse_mode=""
                )
            return
        else:
            # User typed "add" or "add wallet" without address
            await update.message.reply_text(
                "Please provide a wallet address and a nickname to track it, like this: add wallet address nickname.",
                parse_mode=""
            )
            return

    # 3. Remove Wallet
    # Support "remove wallet <nickname>", "remove <nickname>", "delete wallet <nickname>", "delete <nickname>"
    if lower_text in ["remove", "delete", "remove wallet", "delete wallet"]:
        await update.message.reply_text(
            "Please specify the nickname or address of the wallet you want to remove, like this: remove wallet nickname.",
            parse_mode=""
        )
        return

    remove_prefixes = ["remove wallet ", "remove ", "delete wallet ", "delete "]
    for prefix in remove_prefixes:
        if lower_text.startswith(prefix):
            nickname = cleaned_text[len(prefix):].strip()
            if nickname:
                context.user_data["pending_action"] = {
                    "action": "remove_wallet",
                    "nickname": nickname
                }
                msg = f"I understood: remove the wallet named {nickname}. Reply yes to confirm, or no to cancel."
                await update.message.reply_text(msg, parse_mode="")
            else:
                await update.message.reply_text(
                    "Please specify the nickname or address of the wallet you want to remove, like this: remove wallet nickname.",
                    parse_mode=""
                )
            return

    # 4. Profile
    # Support "profile <nickname>"
    if lower_text.startswith("profile "):
        nickname = cleaned_text[len("profile "):].strip()
        if nickname:
            context.args = [nickname]
            await cmd_profile(update, context)
        else:
            await update.message.reply_text(
                "Which wallet would you like a profile for? Please provide its nickname.",
                parse_mode=""
            )
        return

    # Fallback response
    await update.message.reply_text(
        "I did not understand that. Try typing a command like /listwallets or /add wallet.",
        parse_mode=""
    )



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
        BotCommand("profile", "View monitored wallet profile & history"),
        BotCommand("clear", "Clear the conversation history from chat")
    ])
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands registered with Telegram.")
 
 
def build_application():
    """Build and return the python-telegram-bot Application."""
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, PicklePersistence
 
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
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

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
