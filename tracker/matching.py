"""
Similarity matching logic for token names, symbols, and logos.

Thresholds are read from Django settings so they can be tuned without
touching code.  All three check functions return a numeric score:

  check_name / check_symbol  →  0–100 (rapidfuzz ratio; higher = more similar)
  check_logo                 →  int   (imagehash hamming distance; LOWER = more similar)

A pair is considered a match when:
  name   score  ≥  NAME_MATCH_THRESHOLD
  symbol score  ≥  SYMBOL_MATCH_THRESHOLD
  logo   dist   ≤  LOGO_MATCH_THRESHOLD
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import imagehash
import requests
from PIL import Image
from rapidfuzz import fuzz
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """Holds every match signal found between a new buy and a past buy."""
    matched_buy_id: int
    name_score: Optional[float] = None
    symbol_score: Optional[float] = None
    logo_distance: Optional[int] = None

    @property
    def matched(self) -> bool:
        return (
            (self.name_score is not None and self.name_score >= settings.NAME_MATCH_THRESHOLD)
            or (self.symbol_score is not None and self.symbol_score >= settings.SYMBOL_MATCH_THRESHOLD)
        )

    @property
    def match_type(self) -> str:
        parts = []
        if self.name_score is not None and self.name_score >= settings.NAME_MATCH_THRESHOLD:
            parts.append("name")
        if self.symbol_score is not None and self.symbol_score >= settings.SYMBOL_MATCH_THRESHOLD:
            parts.append("symbol")
        if self.logo_distance is not None and self.logo_distance <= settings.LOGO_MATCH_THRESHOLD:
            parts.append("logo")
        return "+".join(parts) if parts else "none"


# ── Individual checks ─────────────────────────────────────────────────────────

def check_name(name_a: str, name_b: str) -> float:
    """
    Return a rapidfuzz token_set_ratio score (0–100).
    token_set_ratio handles word re-ordering and partial matches well,
    so "The White Whale V2" correctly scores high against "The White Whale".
    """
    if not name_a or not name_b:
        return 0.0
    return fuzz.token_set_ratio(name_a.lower(), name_b.lower())


def check_symbol(sym_a: str, sym_b: str) -> float:
    """
    Return a rapidfuzz ratio score (0–100) for symbol comparison.
    Symbols are short uppercase strings so simple ratio is sufficient.
    """
    if not sym_a or not sym_b:
        return 0.0
    return fuzz.ratio(sym_a.upper(), sym_b.upper())


def compute_logo_hash(url: str) -> Optional[str]:
    """
    Download an image from *url* and compute its perceptual hash.
    Returns a hex string, or None if the image cannot be fetched/decoded.
    """
    if not url:
        return None

    # Pre-check URL to avoid downloading SVG files if possible
    if url.lower().split("?")[0].endswith(".svg"):
        logger.debug("SVG logo detected via URL extension; skipping perceptual hash (unsupported by imagehash)")
        return None

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # Check Content-Type header
        content_type = response.headers.get("Content-Type", "")
        if "svg" in content_type:
            logger.debug("SVG logo detected via Content-Type header; skipping perceptual hash (unsupported by imagehash)")
            return None

        img = Image.open(io.BytesIO(response.content)).convert("RGBA")
        h = imagehash.phash(img)
        return str(h)
    except Exception as exc:
        logger.warning("Could not compute logo hash for %s: %s", url, exc)
        return None


def check_logo(hash_a: Optional[str], hash_b: Optional[str]) -> Optional[int]:
    """
    Compare two precomputed imagehash hex strings.
    Returns the Hamming distance (lower = more similar), or None if either
    hash is missing.
    """
    if not hash_a or not hash_b:
        return None
    try:
        return imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b)
    except Exception as exc:
        logger.warning("Logo hash comparison failed: %s", exc)
        return None


# ── Run all checks against a wallet's history ─────────────────────────────────

def run_all_checks(new_buy, past_buys) -> List[MatchResult]:
    """
    Compare *new_buy* (a TokenBuy instance) against every buy in *past_buys*.
    Returns a list of MatchResult objects where .matched is True.

    Skips the new buy itself (same contract_address) to avoid self-matching.
    """
    hits: List[MatchResult] = []

    for past in past_buys:
        # Never match a token against itself
        if past.contract_address == new_buy.contract_address:
            continue

        # Check time difference (exclude concurrent/bundle buys within 15 minutes)
        time_diff = abs(new_buy.timestamp - past.timestamp)
        if time_diff.total_seconds() < 900:  # 15 minutes = 900 seconds
            continue

        result = MatchResult(matched_buy_id=past.pk)

        # Name check
        if new_buy.name and past.name:
            result.name_score = check_name(new_buy.name, past.name)

        # Symbol check
        if new_buy.symbol and past.symbol:
            result.symbol_score = check_symbol(new_buy.symbol, past.symbol)

        # Logo check — use precomputed hashes stored in DB
        if new_buy.logo_hash and past.logo_hash:
            result.logo_distance = check_logo(new_buy.logo_hash, past.logo_hash)

        if result.matched:
            hits.append(result)

    return hits
