from django.db import models
from django.conf import settings


class Wallet(models.Model):
    """A Solana wallet being tracked."""
    address = models.CharField(max_length=44, unique=True)
    nickname = models.CharField(max_length=64, unique=True)
    date_added = models.DateTimeField(auto_now_add=True)
    added_by_telegram_id = models.BigIntegerField()
    last_anomaly_alert_sent = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date_added"]

    def __str__(self):
        return f"{self.nickname} ({self.address[:8]}…)"


class TokenBuy(models.Model):
    """
    A single token-buy event recorded for a wallet.
    logo_hash stores the precomputed perceptual hash string so we
    never re-download the image for future comparisons.
    """
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="buys")
    name = models.CharField(max_length=128, blank=True)
    symbol = models.CharField(max_length=32, blank=True)
    logo_url = models.URLField(max_length=512, blank=True)
    logo_hash = models.CharField(max_length=64, blank=True)  # hex string of imagehash
    contract_address = models.CharField(max_length=44)
    amount = models.DecimalField(max_digits=20, decimal_places=9, null=True, blank=True)
    timestamp = models.DateTimeField()
    tx_signature = models.CharField(max_length=88, null=True, blank=True, db_index=True)
    amount_spent = models.DecimalField(max_digits=20, decimal_places=9, null=True, blank=True)
    spent_symbol = models.CharField(max_length=16, default="SOL")
    creator = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    raw_payload = models.JSONField(default=dict, blank=True)  # full Helius payload for debugging

    class Meta:
        ordering = ["-timestamp"]
        unique_together = ("wallet", "tx_signature")
        indexes = [
            models.Index(fields=["wallet", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.wallet.nickname} bought {self.symbol or self.name} at {self.timestamp:%Y-%m-%d}"


class MatchAlert(models.Model):
    """
    A record of every alert sent.
    new_buy is the trigger; matched_buy is the historical buy it resembles.
    """
    MATCH_TYPE_CHOICES = [
        ("name", "Name"),
        ("symbol", "Symbol"),
        ("logo", "Logo"),
        ("name+symbol", "Name + Symbol"),
        ("name+logo", "Name + Logo"),
        ("symbol+logo", "Symbol + Logo"),
        ("name+symbol+logo", "Name + Symbol + Logo"),
    ]

    new_buy = models.ForeignKey(TokenBuy, on_delete=models.CASCADE, related_name="alerts_as_new")
    matched_buy = models.ForeignKey(TokenBuy, on_delete=models.CASCADE, related_name="alerts_as_match")
    match_type = models.CharField(max_length=32, choices=MATCH_TYPE_CHOICES)
    name_score = models.FloatField(null=True, blank=True)
    symbol_score = models.FloatField(null=True, blank=True)
    logo_distance = models.IntegerField(null=True, blank=True)  # lower = more similar
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"Alert: {self.new_buy} matched {self.matched_buy} via {self.match_type}"
