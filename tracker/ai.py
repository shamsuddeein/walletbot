"""
AI-powered intelligence layer for the wallet tracker.

Three public functions:
  get_ai_explanation()   — explains a match alert in plain language
  get_token_risk()       — scores a new token buy for risk using DexScreener data
  get_wallet_context()   — summarises what a wallet's history says about a new buy
  understand_message()   — interprets a plain-English message and returns an action or reply

All functions fail gracefully. A failed AI call never blocks an alert or command.
"""
from __future__ import annotations

import logging
import requests

from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"

# Try primary model first, fall back to secondary if rate-limited
MODELS = [
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
]


def _call_ai(prompt_or_messages: str | list[dict], system: str = "") -> str:
    """
    Send a prompt or list of messages to OpenRouter and return the response text.
    Tries each model in MODELS in order. Returns "" on total failure.
    """
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key:
        return ""

    if isinstance(prompt_or_messages, list):
        messages = list(prompt_or_messages)
        if system and not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system})
    else:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt_or_messages})

    try:
        for model in MODELS:
            resp = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages},
                timeout=20,
            )
            data = resp.json()
            if "choices" in data:
                content = data["choices"][0]["message"].get("content")
                if content is not None:
                    return str(content).strip()
            logger.warning("Model %s unavailable: %s", model, data.get("error", {}).get("message", ""))
        logger.warning("All AI models failed.")
        return ""
    except Exception as exc:
        logger.warning("AI call failed (non-fatal): %s", exc)
        return ""


# ── Public functions ───────────────────────────────────────────────────────────

def get_ai_explanation(
    new_name: str,
    new_symbol: str,
    past_name: str,
    past_symbol: str,
    time_diff: str,
    match_reason: str,
    wallet_nickname: str,
) -> str:
    """
    Explain what a token match pattern likely means in plain language.
    Returns 2-3 sentences or "" on failure.
    """
    prompt = (
        f"A crypto wallet called '{wallet_nickname}' just bought a token that resembles one it bought before.\n\n"
        f"New token: {new_name} ({new_symbol})\n"
        f"Previously bought: {past_name} ({past_symbol})\n"
        f"Time between buys: {time_diff}\n"
        f"Match reason: {match_reason}\n\n"
        f"In 2 to 3 plain sentences, explain what this pattern could mean. "
        f"Be direct and realistic. Focus on what the repetition signals about the wallet's strategy."
    )
    return _call_ai(prompt)


def get_token_risk(name: str, symbol: str, contract_address: str) -> dict:
    """
    Fetch live DexScreener data for a token and score its risk.
    Uses rules-based logic by default, falling back to AI if configured.

    Returns a dict:
        {
            "level": "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN",
            "reason": "plain English explanation",
            "dex_data": {...raw DexScreener summary...}
        }
    """
    from django.core.cache import cache
    
    cache_key = f"token_risk_{contract_address}"
    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info("Found cached token risk for %s", contract_address)
        return cached_result

    # Fetch DexScreener data
    dex_summary = {}
    try:
        r = requests.get(f"{DEXSCREENER_URL}/{contract_address}", timeout=10)
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            p = pairs[0]
            dex_summary = {
                "age_hours": _pair_age_hours(p),
                "liquidity_usd": p.get("liquidity", {}).get("usd", 0),
                "volume_24h": p.get("volume", {}).get("h24", 0),
                "price_change_24h": p.get("priceChange", {}).get("h24", 0),
                "market_cap": p.get("marketCap", 0),
                "dex": p.get("dexId", "unknown"),
            }
    except Exception as exc:
        logger.warning("DexScreener fetch failed for %s: %s", contract_address, exc)

    if not dex_summary:
        return {"level": "UNKNOWN", "reason": "Could not fetch market data.", "dex_data": {}}

    age = dex_summary.get("age_hours", 999)
    liq = dex_summary.get("liquidity_usd", 0)
    vol = dex_summary.get("volume_24h", 0)

    # Rules-based deterministic risk assessment (instant & free)
    if liq < 5000:
        level = "HIGH"
        reason = f"Extremely low liquidity (${liq:,.0f}). High risk of dump or inability to sell."
    elif age < 1:
        level = "HIGH"
        reason = f"Token is less than 1 hour old ({age:.1f}h). High risk of sudden rug pull."
    elif liq > 50000 and age > 24:
        level = "LOW"
        reason = f"Healthy liquidity (${liq:,.0f}) and has been active for over 24 hours."
    else:
        level = "MEDIUM"
        reason = f"Moderate liquidity (${liq:,.0f}) and age ({age:.1f} hours)."

    # Only call AI if OpenRouter key is set
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if api_key:
        prompt = (
            f"You are a crypto risk analyst. Rate the risk of this token for a trader:\n\n"
            f"Token: {name} ({symbol})\n"
            f"Age: {age:.1f} hours\n"
            f"Liquidity: ${liq:,.0f}\n"
            f"24h volume: ${vol:,.0f}\n"
            f"24h price change: {dex_summary.get('price_change_24h', 0):.1f}%\n"
            f"Market cap: ${dex_summary.get('market_cap', 0):,.0f}\n"
            f"DEX: {dex_summary.get('dex', 'unknown')}\n\n"
            f"Reply with EXACTLY this format:\n"
            f"LEVEL: HIGH or MEDIUM or LOW\n"
            f"REASON: one sentence explanation"
        )
        ai_response = _call_ai(prompt)
        if ai_response:
            for line in ai_response.splitlines():
                line = line.strip()
                if line.upper().startswith("LEVEL:"):
                    raw = line.split(":", 1)[1].strip().upper()
                    if "HIGH" in raw:
                        level = "HIGH"
                    elif "LOW" in raw:
                        level = "LOW"
                    elif "MEDIUM" in raw:
                        level = "MEDIUM"
                elif line.upper().startswith("REASON:"):
                    reason = line.split(":", 1)[1].strip()

    result = {"level": level, "reason": reason, "dex_data": dex_summary}
    cache.set(cache_key, result, 600)  # cache for 10 mins
    return result


def get_wallet_context(wallet_nickname: str, recent_buys: list[dict]) -> str:
    """
    Given a wallet's recent buy history, ask the AI what the latest buy suggests
    about its current strategy.

    recent_buys: list of dicts with keys: name, symbol, timestamp_str
    Returns a single sentence or "" on failure.
    """
    if not recent_buys:
        return ""

    history_lines = "\n".join(
        f"- {b['name']} ({b['symbol']}) — {b['timestamp_str']}"
        for b in recent_buys[:15]
    )

    prompt = (
        f"Wallet '{wallet_nickname}' has this recent buy history (newest first):\n"
        f"{history_lines}\n\n"
        f"In one sentence, what does this history suggest about what the wallet is doing right now?"
    )
    return _call_ai(prompt)


def understand_message(
    user_text: str,
    history: list[dict],
    wallet_names: list[str],
    total_buys: int,
    alerts_today: int,
) -> dict:
    """
    Interpret a plain-English message from the user, incorporating conversation history,
    and return either:
      {"type": "reply", "text": "..."}           — bot should send this text
      {"type": "action", "action": "list_wallets"}
      {"type": "action", "action": "add_wallet",    "address": "...", "nickname": "..."}
      {"type": "action", "action": "remove_wallet", "nickname": "..."}
      {"type": "action", "action": "profile",       "nickname": "..."}

    Returns {"type": "reply", "text": "..."} on failure.
    """
    wallets_str = ", ".join(wallet_names) if wallet_names else "none"

    system = (
        "You are the AI brain of a Solana wallet tracker Telegram bot. "
        "This bot runs entirely inside a Telegram chat. There is NO web app, NO dashboard UI, and NO buttons/screens. "
        "If the user asks how to do something, explain they must type commands or requests directly in this chat. "
        "\n\n"
        "CRITICAL RULE ON ADDING WALLETS:\n"
        "Only trigger 'add_wallet' if you have BOTH a valid Solana address (base58, 32-44 chars) AND a nickname. "
        "If you have an address but no nickname, do NOT trigger 'add_wallet'. Instead, reply asking what nickname "
        "they want for that address. "
        "If you have a nickname but no address, do NOT trigger 'add_wallet'. Instead, reply asking for the address. "
        "Never use placeholder nicknames like 'Unknown' or 'My Wallet' unless the user explicitly requested it. "
        "\n\n"
        "CRITICAL RULE ON CONVERSATION HISTORY:\n"
        "Use the provided conversation history to understand context. If you previously asked for a nickname "
        "and the user replies with a name (e.g. 'Shamo'), use the previous messages in history to connect it to "
        "the address they provided before, and then return the 'add_wallet' action with both values."
    )

    # Build history context for the message API
    messages = []
    for msg in history[-8:]:  # last 8 messages
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Append the current prompt
    prompt = (
        f"User message: \"{user_text}\"\n\n"
        f"Current state:\n"
        f"- Tracked wallets: {wallets_str}\n"
        f"- Total buys recorded: {total_buys}\n"
        f"- Alerts fired today: {alerts_today}\n\n"
        f"Decide what to do. Reply in EXACTLY one of these formats:\n\n"
        f"If the user wants to list wallets:\n"
        f"ACTION: list_wallets\n\n"
        f"If the user wants to add a wallet (must have address AND nickname):\n"
        f"ACTION: add_wallet\n"
        f"ADDRESS: <solana address>\n"
        f"NICKNAME: <nickname>\n\n"
        f"If the user wants to remove a wallet:\n"
        f"ACTION: remove_wallet\n"
        f"NICKNAME: <nickname>\n\n"
        f"If the user wants to see a wallet profile:\n"
        f"ACTION: profile\n"
        f"NICKNAME: <nickname>\n\n"
        f"If the user just wants a conversational answer, or you need to ask for missing info (nickname/address):\n"
        f"REPLY: <your answer in 1-3 sentences>"
    )
    messages.append({"role": "user", "content": prompt})

    response = _call_ai(messages, system=system)
    if not response:
        return {"type": "reply", "text": "Sorry, I could not process that right now. Try a command like /listwallets."}

    lines = [l.strip() for l in response.splitlines() if l.strip()]
    result: dict = {}

    for line in lines:
        upper = line.upper()
        if upper.startswith("ACTION:"):
            result["type"] = "action"
            result["action"] = line.split(":", 1)[1].strip(" '\"[]()").lower()
        elif upper.startswith("ADDRESS:"):
            result["address"] = line.split(":", 1)[1].strip(" '\"[]()")
        elif upper.startswith("NICKNAME:"):
            result["nickname"] = line.split(":", 1)[1].strip(" '\"[]()")
        elif upper.startswith("REPLY:"):
            result["type"] = "reply"
            result["text"] = line.split(":", 1)[1].strip()

    if not result.get("type"):
        return {"type": "reply", "text": response}

    return result


def generate_daily_digest(wallet_summaries: list[dict]) -> str:
    """
    Generate a morning digest message from a list of wallet summaries.

    Each summary dict:
        {"nickname": str, "buys_24h": int, "alerts_24h": int, "tokens": [str]}

    Returns a formatted multi-line message or "" on failure.
    """
    if not wallet_summaries:
        return ""

    lines = []
    for w in wallet_summaries:
        tokens = ", ".join(w.get("tokens", [])) or "none"
        lines.append(
            f"Wallet {w['nickname']}: {w['buys_24h']} buy(s) — {w['alerts_24h']} alert(s). "
            f"Tokens: {tokens}"
        )

    summary_text = "\n".join(lines)

    prompt = (
        f"Good morning. Here is what happened in the last 24 hours across tracked wallets:\n\n"
        f"{summary_text}\n\n"
        f"Write a short digest (4-6 sentences) summarising the activity in plain language. "
        f"Highlight anything unusual. Be direct and useful, not generic."
    )
    return _call_ai(prompt)


def generate_wallet_profile(wallet_nickname: str, buy_history: list[dict], alert_count: int) -> str:
    """
    Generate or refresh a wallet's AI profile based on its full buy history.

    buy_history: list of {"name", "symbol", "timestamp_str"} dicts (most recent first)
    Returns a profile paragraph or "" on failure.
    """
    if not buy_history:
        return ""

    history_lines = "\n".join(
        f"- {b['name']} ({b['symbol']}) at {b['timestamp_str']}"
        for b in buy_history[:30]
    )

    prompt = (
        f"Wallet '{wallet_nickname}' has made {len(buy_history)} recorded buys. "
        f"{alert_count} of those triggered similarity alerts.\n\n"
        f"Buy history (newest first):\n{history_lines}\n\n"
        f"Write a 3-4 sentence profile of this wallet's trading personality and strategy. "
        f"Be specific about patterns you can actually see in the data. No guessing."
    )
    return _call_ai(prompt)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _pair_age_hours(pair: dict) -> float:
    """Calculate how old a DexScreener pair is in hours."""
    import time
    created_at = pair.get("pairCreatedAt")
    if not created_at:
        return 9999.0
    try:
        age_ms = time.time() * 1000 - float(created_at)
        return age_ms / 3_600_000
    except Exception:
        return 9999.0
