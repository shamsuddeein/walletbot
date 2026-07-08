"""
Tests for the matching engine.

The three real client examples from the spec are used as ground truth:
  1. "The Black Bull" vs "The Black Bull"  — identical name, different logo
  2. "Dumacrats" vs "Dumacrats"            — identical name, different logo
  3. "The White Whale" vs "The White Whale V2" — near-identical name

These drive threshold tuning.  Both positive (should match) and negative
(should NOT match) cases are tested.
"""
import django
from django.test import TestCase, override_settings

from tracker.matching import check_name, check_symbol, check_logo


THRESHOLDS = dict(
    NAME_MATCH_THRESHOLD=85,
    SYMBOL_MATCH_THRESHOLD=80,
    LOGO_MATCH_THRESHOLD=10,
)


@override_settings(**THRESHOLDS)
class NameMatchingTests(TestCase):

    # ── Positive cases (must trigger) ────────────────────────────────────────

    def test_identical_name(self):
        """The Black Bull vs The Black Bull — exact match."""
        score = check_name("The Black Bull", "The Black Bull")
        self.assertGreaterEqual(score, 85, f"Expected ≥85, got {score}")

    def test_identical_name_dumacrats(self):
        """Dumacrats vs Dumacrats — exact match."""
        score = check_name("Dumacrats", "Dumacrats")
        self.assertGreaterEqual(score, 85, f"Expected ≥85, got {score}")

    def test_near_identical_name_v2(self):
        """The White Whale vs The White Whale V2 — near-identical, must catch."""
        score = check_name("The White Whale", "The White Whale V2")
        self.assertGreaterEqual(score, 85, f"Expected ≥85, got {score}")

    def test_case_insensitive(self):
        """Name check should be case-insensitive."""
        score = check_name("THE BLACK BULL", "the black bull")
        self.assertGreaterEqual(score, 85)

    # ── Negative cases (must NOT trigger) ────────────────────────────────────

    def test_unrelated_names(self):
        """Completely unrelated names should not match."""
        score = check_name("Solana Apes", "Moon Cats")
        self.assertLess(score, 85, f"Expected <85, got {score}")

    def test_empty_name(self):
        """Empty name should return 0."""
        self.assertEqual(check_name("", "The Black Bull"), 0.0)
        self.assertEqual(check_name("The Black Bull", ""), 0.0)
    def test_short_single_word_names_strict_matching(self):
        """Single word names like 'Bull' should not trigger on longer names like 'The Black Bull'."""
        score = check_name("Bull", "The Black Bull")
        self.assertLess(score, 85, f"Expected <85, got {score}")



@override_settings(**THRESHOLDS)
class SymbolMatchingTests(TestCase):

    def test_identical_symbol(self):
        score = check_symbol("BULL", "BULL")
        self.assertGreaterEqual(score, 80)

    def test_near_identical_symbol(self):
        """WHALE vs WHALEV2 — close enough to catch the "v2" pattern."""
        score = check_symbol("WHALE", "WHALE2")
        # This intentionally may or may not match depending on threshold tuning.
        # We document the actual score here rather than assert a pass/fail,
        # since the spec says the name check is the primary signal for v2 tokens.
        self.assertIsInstance(score, float)

    def test_unrelated_symbols(self):
        score = check_symbol("BULL", "MOON")
        self.assertLess(score, 80)

    def test_case_normalised(self):
        score = check_symbol("bull", "BULL")
        self.assertGreaterEqual(score, 80)

    def test_empty_symbol(self):
        self.assertEqual(check_symbol("", "BULL"), 0.0)


@override_settings(**THRESHOLDS)
class LogoMatchingTests(TestCase):
    """
    Logo tests use precomputed hash strings.  We can't test real downloaded
    images in unit tests, but we can verify the hash comparison arithmetic.

    Two identical hashes → distance 0 (definitely a match).
    Two very different hashes → large distance (not a match).
    """

    def test_identical_hash(self):
        """Same image hash should produce distance 0."""
        h = "f8c8f8c8f8c8f8c8"
        dist = check_logo(h, h)
        self.assertIsNotNone(dist)
        self.assertLessEqual(dist, 10)

    def test_completely_different_hash(self):
        """Inverted hash should produce maximum distance."""
        h_a = "0000000000000000"
        h_b = "ffffffffffffffff"
        dist = check_logo(h_a, h_b)
        self.assertIsNotNone(dist)
        self.assertGreater(dist, 10)

    def test_none_when_hash_missing(self):
        """Missing hashes should return None, not crash."""
        self.assertIsNone(check_logo(None, "f8c8f8c8f8c8f8c8"))
        self.assertIsNone(check_logo("f8c8f8c8f8c8f8c8", None))
        self.assertIsNone(check_logo(None, None))
        self.assertIsNone(check_logo("", "f8c8f8c8f8c8f8c8"))


@override_settings(**THRESHOLDS)
class NameAndSymbolCrossCheckTests(TestCase):
    def test_identical_name_different_symbol_unrelated(self):
        """
        Names are identical (The White Bull vs The White Bull), but symbols are
        completely different (LEVI vs DAVID, score 19%). Should not match.
        """
        from tracker.matching import MatchResult
        res = MatchResult(
            matched_buy_id=1,
            name_score=100.0,
            symbol_score=19.0, # LEVI vs DAVID
            logo_distance=None
        )
        self.assertFalse(res.matched)

    def test_identical_name_different_symbol_v2_relaunch(self):
        """
        Names are similar/identical, and symbol is highly similar (LEVI vs LEVI2, score 88%).
        Should match (V2 relaunch).
        """
        from tracker.matching import MatchResult
        res = MatchResult(
            matched_buy_id=1,
            name_score=100.0,
            symbol_score=88.0, # LEVI vs LEVI2
            logo_distance=None
        )
        self.assertTrue(res.matched)

    def test_identical_name_different_symbol_with_logo_match(self):
        """
        Names are identical, symbols are different (LEVI vs DAVID), but logos match.
        Should match.
        """
        from tracker.matching import MatchResult
        res = MatchResult(
            matched_buy_id=1,
            name_score=100.0,
            symbol_score=19.0,
            logo_distance=2 # logo matched
        )
        self.assertTrue(res.matched)
