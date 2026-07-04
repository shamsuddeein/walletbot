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
        addresses = ["nwPkEagtaEE36tXW1y7ocozuphko1od75DMY7nPuupuE"]

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
