from django.test import TestCase
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.utils import timezone
from decimal import Decimal

from tracker.models import Wallet, TokenBuy, MatchAlert
from tracker.telegram_bot import send_alert
from tracker.tasks import process_buy_event


class NewFixesTests(TestCase):
    def setUp(self):
        # Create test wallet
        self.wallet = Wallet.objects.create(
            address="TestWalletAddress1111111111111111111111111",
            nickname="Test_Wallet",
            added_by_telegram_id=12345,
        )
        self.now = timezone.now()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_alert_uses_photo_when_logo_present(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        # Setup mock MatchAlert
        past = TokenBuy.objects.create(
            wallet=self.wallet,
            name="PastToken",
            symbol="PAST",
            contract_address="past_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
        )
        new = TokenBuy.objects.create(
            wallet=self.wallet,
            name="NewToken",
            symbol="NEW",
            logo_url="http://example.com/logo.png",
            contract_address="new_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.5"),
            amount=Decimal("150.0"),
            spent_symbol="SOL",
        )
        alert = MatchAlert.objects.create(
            new_buy=new,
            matched_buy=past,
            match_type="name",
            name_score=90.0,
        )

        mock_send_photo.return_value = True

        # Send alert
        success = send_alert(alert)

        self.assertTrue(success)
        mock_send_photo.assert_called_once()
        mock_send_msg.assert_not_called()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_alert_falls_back_on_photo_failure(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        # Setup mock MatchAlert
        past = TokenBuy.objects.create(
            wallet=self.wallet,
            name="PastToken",
            symbol="PAST",
            contract_address="past_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
        )
        new = TokenBuy.objects.create(
            wallet=self.wallet,
            name="NewToken",
            symbol="NEW",
            logo_url="http://example.com/logo.png",
            contract_address="new_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.5"),
            amount=Decimal("150.0"),
            spent_symbol="SOL",
        )
        alert = MatchAlert.objects.create(
            new_buy=new,
            matched_buy=past,
            match_type="name",
            name_score=90.0,
        )

        mock_send_photo.return_value = False
        mock_send_msg.return_value = True

        # Send alert
        success = send_alert(alert)

        self.assertTrue(success)
        mock_send_photo.assert_called_once()
        mock_send_msg.assert_called_once()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_alert_no_photo_when_logo_missing(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        # Setup mock MatchAlert
        past = TokenBuy.objects.create(
            wallet=self.wallet,
            name="PastToken",
            symbol="PAST",
            contract_address="past_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
        )
        new = TokenBuy.objects.create(
            wallet=self.wallet,
            name="NewToken",
            symbol="NEW",
            logo_url="", # empty
            contract_address="new_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.5"),
            amount=Decimal("150.0"),
            spent_symbol="SOL",
        )
        alert = MatchAlert.objects.create(
            new_buy=new,
            matched_buy=past,
            match_type="name",
            name_score=90.0,
        )

        mock_send_msg.return_value = True

        # Send alert
        success = send_alert(alert)

        self.assertTrue(success)
        mock_send_photo.assert_not_called()
        mock_send_msg.assert_called_once()

    @patch("tracker.ai.get_token_risk")
    @patch("tracker.telegram_bot.send_alert")
    @patch("tracker.helius.get_token_creator", return_value="creator_addr")
    def test_liquidity_filter_skips_alert_below_500(self, mock_creator, mock_send_alert, mock_get_risk):
        mock_get_risk.return_value = {
            "level": "HIGH",
            "reason": "low liquidity",
            "dex_data": {
                "liquidity_usd": 499, # below 500
            }
        }
        # Prepare past buy
        TokenBuy.objects.create(
            wallet=self.wallet,
            name="Bull Token",
            symbol="BULL",
            contract_address="past_addr",
            timestamp=self.now - timedelta(days=1),
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
        )

        # Mock payload
        payload = {
            "type": "SWAP",
            "signature": "sig123",
            "timestamp": int(self.now.timestamp()),
            "tokenTransfers": [
                {
                    "toUserAccount": self.wallet.address,
                    "mint": "new_addr",
                    "tokenAmount": 100.0,
                    "tokenName": "Bull Token V2",
                    "tokenSymbol": "BULL2",
                    "tokenIcon": "http://logo.url"
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": self.wallet.address,
                    "amount": 1000000000
                }
            ]
        }

        # Run task process
        process_buy_event(payload)

        # MatchAlert should be saved to database
        self.assertEqual(MatchAlert.objects.count(), 1)
        # But send_alert should NOT have been called
        mock_send_alert.assert_not_called()

    @patch("tracker.ai.get_token_risk")
    @patch("tracker.telegram_bot.send_alert")
    @patch("tracker.helius.get_token_creator", return_value="creator_addr")
    def test_liquidity_filter_sends_alert_above_500(self, mock_creator, mock_send_alert, mock_get_risk):
        mock_get_risk.return_value = {
            "level": "MEDIUM",
            "reason": "good liquidity",
            "dex_data": {
                "liquidity_usd": 501, # above 500
            }
        }
        # Prepare past buy
        TokenBuy.objects.create(
            wallet=self.wallet,
            name="Bull Token",
            symbol="BULL",
            contract_address="past_addr",
            timestamp=self.now - timedelta(days=1),
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
        )

        # Mock payload
        payload = {
            "type": "SWAP",
            "signature": "sig456",
            "timestamp": int(self.now.timestamp()),
            "tokenTransfers": [
                {
                    "toUserAccount": self.wallet.address,
                    "mint": "new_addr2",
                    "tokenAmount": 100.0,
                    "tokenName": "Bull Token V2",
                    "tokenSymbol": "BULL2",
                    "tokenIcon": "http://logo.url"
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": self.wallet.address,
                    "amount": 1000000000
                }
            ]
        }

        # Run task process
        process_buy_event(payload)

        # MatchAlert should be saved to database
        self.assertEqual(MatchAlert.objects.count(), 1)
        # and send_alert should have been called
        mock_send_alert.assert_called_once()
