"""
Helius API integration.

Handles:
  - Registering / unregistering wallet addresses on the webhook
  - Fetching token metadata as a fallback when the webhook payload is sparse
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

HELIUS_BASE = "https://api.helius.xyz/v0"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"


# ── Webhook management ────────────────────────────────────────────────────────

def _get_webhook() -> Optional[dict]:
    """Fetch the current webhook config from Helius."""
    if not settings.HELIUS_WEBHOOK_ID:
        return None
    url = f"{HELIUS_BASE}/webhooks/{settings.HELIUS_WEBHOOK_ID}?api-key={settings.HELIUS_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.error("Failed to fetch Helius webhook: %s", exc)
        return None


def _update_webhook_addresses(addresses: list[str]) -> bool:
    """
    Replace the webhook's watched-address list with *addresses*.
    Returns True on success.
    """
    if not settings.HELIUS_WEBHOOK_ID or not settings.HELIUS_API_KEY:
        logger.warning("Helius credentials not configured — skipping webhook sync.")
        return False

    is_active = len(addresses) > 0
    # Helius webhooks require at least 1 watch address even if paused/inactive.
    # If the list is empty, supply our dead placeholder address.
    if not addresses:
        addresses = ["6xuDH1sVu61i2fidPc5wczdLHqmZYmMYrYZacmYbS9T2"]

    url = f"{HELIUS_BASE}/webhooks/{settings.HELIUS_WEBHOOK_ID}?api-key={settings.HELIUS_API_KEY}"
    payload = {
        "webhookURL": f"{settings.WEBHOOK_BASE_URL}/webhook/helius/",
        "transactionTypes": ["SWAP"],
        "accountAddresses": addresses,
        "webhookType": "enhanced",
        "active": is_active,
    }
    try:
        r = requests.put(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.error(
                "Failed to update Helius webhook (Status Code %d). Helius Response: %s",
                r.status_code,
                r.text
            )
            return False
        logger.info("Helius webhook updated with %d address(es).", len(addresses))
        return True
    except Exception as exc:
        logger.exception("HTTP Request to Helius API failed: %s", exc)
        return False


def register_wallet(address: str) -> bool:
    """Add *address* to the Helius webhook's watched list."""
    from tracker.models import Wallet  # avoid circular import at module level

    current_addresses = list(Wallet.objects.values_list("address", flat=True))
    if address not in current_addresses:
        current_addresses.append(address)
    return _update_webhook_addresses(current_addresses)


def unregister_wallet(address: str) -> bool:
    """Remove *address* from the Helius webhook's watched list."""
    from tracker.models import Wallet

    current_addresses = list(
        Wallet.objects.exclude(address=address).values_list("address", flat=True)
    )
    return _update_webhook_addresses(current_addresses)


def create_webhook(webhook_url: str) -> Optional[str]:
    """
    Create a brand-new Helius webhook and return its ID.
    Call this once during initial setup; store the returned ID in .env as
    HELIUS_WEBHOOK_ID.
    """
    url = f"{HELIUS_BASE}/webhooks?api-key={settings.HELIUS_API_KEY}"
    payload = {
        "webhookURL": webhook_url,
        "transactionTypes": ["SWAP"],
        "accountAddresses": [],
        "webhookType": "enhanced",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        webhook_id = data.get("webhookID")
        logger.info("Created Helius webhook: %s", webhook_id)
        return webhook_id
    except Exception as exc:
        logger.error("Failed to create Helius webhook: %s", exc)
        return None


# ── Token metadata fallback ───────────────────────────────────────────────────

def get_token_metadata(mint_address: str) -> dict:
    """
    Fetch token metadata from Helius's getAsset RPC method.
    Returns a dict with keys: name, symbol, logo_url (all may be empty strings).
    Used only when the webhook payload doesn't include full metadata.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": "walletbot",
        "method": "getAsset",
        "params": {"id": mint_address},
    }
    try:
        r = requests.post(HELIUS_RPC, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", {})
        content = data.get("content", {})
        metadata = content.get("metadata", {})
        files = content.get("files", [])
        logo_url = ""
        for f in files:
            if f.get("mime", "").startswith("image/"):
                logo_url = f.get("uri", "")
                break
        return {
            "name": metadata.get("name", ""),
            "symbol": metadata.get("symbol", ""),
            "logo_url": logo_url,
        }
    except Exception as exc:
        logger.warning("Could not fetch metadata for %s: %s", mint_address, exc)
        return {"name": "", "symbol": "", "logo_url": ""}


def get_token_creator(mint_address: str) -> Optional[str]:
    """
    Fetch the developer (creator) wallet address of a mint using Helius RPC.
    Paginates backwards to find the creation transaction and extracts the signer.
    """
    url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    before = None
    oldest_sig = None

    # Paginate up to 3 times (3000 signatures) to locate the creation signature
    for _ in range(3):
        params = {"limit": 1000}
        if before:
            params["before"] = before
        payload = {
            "jsonrpc": "2.0",
            "id": "get-sigs",
            "method": "getSignaturesForAddress",
            "params": [mint_address, params]
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            res = r.json().get("result", [])
            if not res:
                break
            oldest_sig = res[-1].get("signature")
            before = oldest_sig
            if len(res) < 1000:
                break
        except Exception as e:
            logger.warning("Error fetching signatures for creator lookup: %s", e)
            break

    if not oldest_sig:
        return None

    # Fetch transaction to extract the fee-paying signer (developer)
    tx_payload = {
        "jsonrpc": "2.0",
        "id": "get-tx",
        "method": "getTransaction",
        "params": [oldest_sig, {"maxSupportedTransactionVersion": 0}]
    }
    try:
        r = requests.post(url, json=tx_payload, timeout=10)
        tx_data = r.json().get("result", {})
        if tx_data:
            message = tx_data.get("transaction", {}).get("message", {})
            account_keys = message.get("accountKeys", [])
            if not account_keys:
                account_keys = message.get("staticAccountKeys", [])
            if account_keys:
                return account_keys[0]  # First key is the signer/creator
    except Exception as e:
        logger.warning("Error fetching transaction details for creator lookup: %s", e)
    return None


def get_token_holders_distribution(mint_address: str) -> Optional[dict]:
    """
    Fetch the top 20 token holders and calculate concentration statistics.
    Returns a dict:
    {
        "total_supply": float,
        "top_10_percent": float,
        "top_20_percent": float,
        "holders": [
            {"owner": str, "balance": float, "percentage": float},
            ...
        ]
    }
    """
    url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    
    # 1. Fetch total supply
    supply_payload = {
        "jsonrpc": "2.0",
        "id": "get-supply",
        "method": "getTokenSupply",
        "params": [mint_address]
    }
    try:
        r = requests.post(url, json=supply_payload, timeout=10)
        res = r.json().get("result", {})
        supply_data = res.get("value", {})
        total_supply = supply_data.get("uiAmount") or 0.0
    except Exception as e:
        logger.warning("Error fetching token supply for %s: %s", mint_address, e)
        total_supply = 0.0

    if total_supply <= 0:
        return None

    # 2. Fetch largest token accounts
    largest_payload = {
        "jsonrpc": "2.0",
        "id": "get-largest",
        "method": "getTokenLargestAccounts",
        "params": [mint_address]
    }
    try:
        r = requests.post(url, json=largest_payload, timeout=10)
        largest_accounts = r.json().get("result", {}).get("value", [])
    except Exception as e:
        logger.warning("Error fetching largest token accounts for %s: %s", mint_address, e)
        largest_accounts = []

    if not largest_accounts:
        return None

    # Extract account pubkeys
    pubkeys = [account["address"] for account in largest_accounts]

    # 3. Resolve owner wallets using getMultipleAccounts with jsonParsed
    accounts_payload = {
        "jsonrpc": "2.0",
        "id": "get-multiple",
        "method": "getMultipleAccounts",
        "params": [
            pubkeys,
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        r = requests.post(url, json=accounts_payload, timeout=10)
        accounts_data = r.json().get("result", {}).get("value", [])
    except Exception as e:
        logger.warning("Error resolving account owners for %s: %s", mint_address, e)
        accounts_data = []

    # Parse holder records
    holders = []
    for i, acc in enumerate(accounts_data):
        if not acc:
            continue
        data = acc.get("data")
        if isinstance(data, dict) and data.get("program") == "spl-token":
            parsed_info = data.get("parsed", {}).get("info", {})
            owner = parsed_info.get("owner")
            ui_amount = parsed_info.get("tokenAmount", {}).get("uiAmount") or 0.0
            if owner and ui_amount > 0:
                percentage = (ui_amount / total_supply) * 100.0 if total_supply > 0 else 0.0
                holders.append({
                    "owner": owner,
                    "balance": ui_amount,
                    "percentage": percentage
                })
        else:
            # Fallback if parsing failed but we had the uiAmount from getTokenLargestAccounts
            if i < len(largest_accounts):
                ui_amount = largest_accounts[i].get("uiAmount") or 0.0
                percentage = (ui_amount / total_supply) * 100.0 if total_supply > 0 else 0.0
                holders.append({
                    "owner": "unknown (token account: " + pubkeys[i][:8] + "...)",
                    "balance": ui_amount,
                    "percentage": percentage
                })

    # Sort holders by balance descending
    holders.sort(key=lambda x: x["balance"], reverse=True)

    # Calculate concentration stats
    top_10_sum = sum(h["percentage"] for h in holders[:10])
    top_20_sum = sum(h["percentage"] for h in holders[:20])

    return {
        "total_supply": total_supply,
        "top_10_percent": top_10_sum,
        "top_20_percent": top_20_sum,
        "holders": holders
    }


def get_creator_token_balance(mint_address: str, creator_address: str) -> float:
    """Fetch the total token balance owned by the creator wallet."""
    if not creator_address:
        return 0.0
    
    url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "get-creator-balance",
        "method": "getTokenAccountsByOwner",
        "params": [
            creator_address,
            {"mint": mint_address},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        value = r.json().get("result", {}).get("value", [])
        balance = 0.0
        for acc in value:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amount = info.get("tokenAmount", {}).get("uiAmount") or 0.0
            balance += amount
        return balance
    except Exception as e:
        logger.warning("Error fetching creator token balance: %s", e)
        return 0.0


def get_mint_security_info(mint_address: str) -> Optional[dict]:
    """Fetch mint information to verify authorities (mint/freeze authority revoked status)."""
    url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "get-mint-info",
        "method": "getMultipleAccounts",
        "params": [
            [mint_address],
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        value = r.json().get("result", {}).get("value", [])
        if value and value[0]:
            data = value[0].get("data")
            if isinstance(data, dict) and data.get("program") == "spl-token":
                info = data.get("parsed", {}).get("info", {})
                return {
                    "mint_authority": info.get("mintAuthority"),
                    "freeze_authority": info.get("freezeAuthority"),
                }
    except Exception as e:
        logger.warning("Error fetching mint info for %s: %s", mint_address, e)
    return None

