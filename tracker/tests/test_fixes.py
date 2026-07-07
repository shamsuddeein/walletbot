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

    @patch("requests.post")
    def test_get_token_holders_distribution(self, mock_post):
        from tracker.helius import get_token_holders_distribution
        
        mock_supply_res = MagicMock()
        mock_supply_res.json.return_value = {
            "result": {
                "value": {
                    "uiAmount": 1000000.0
                }
            }
        }
        
        mock_largest_res = MagicMock()
        mock_largest_res.json.return_value = {
            "result": {
                "value": [
                    {"address": "acc1", "uiAmount": 500000.0},
                    {"address": "acc2", "uiAmount": 200000.0},
                ]
            }
        }
        
        mock_multiple_res = MagicMock()
        mock_multiple_res.json.return_value = {
            "result": {
                "value": [
                    {
                        "data": {
                            "program": "spl-token",
                            "parsed": {
                                "info": {
                                    "owner": "wallet1",
                                    "tokenAmount": {
                                        "uiAmount": 500000.0
                                    }
                                }
                            }
                        }
                    },
                    {
                        "data": {
                            "program": "spl-token",
                            "parsed": {
                                "info": {
                                    "owner": "wallet2",
                                    "tokenAmount": {
                                        "uiAmount": 200000.0
                                    }
                                }
                            }
                        }
                    }
                ]
            }
        }
        
        mock_post.side_effect = [mock_supply_res, mock_largest_res, mock_multiple_res]
        
        dist = get_token_holders_distribution("some_mint")
        
        self.assertIsNotNone(dist)
        self.assertEqual(dist["total_supply"], 1000000.0)
        self.assertEqual(dist["top_10_percent"], 70.0)
        self.assertEqual(len(dist["holders"]), 2)
        self.assertEqual(dist["holders"][0]["owner"], "wallet1")
        self.assertEqual(dist["holders"][0]["percentage"], 50.0)
        self.assertEqual(dist["holders"][1]["owner"], "wallet2")
        self.assertEqual(dist["holders"][1]["percentage"], 20.0)

    @patch("requests.post")
    def test_get_creator_token_balance(self, mock_post):
        from tracker.helius import get_creator_token_balance
        
        mock_res = MagicMock()
        mock_res.json.return_value = {
            "result": {
                "value": [
                    {
                        "account": {
                            "data": {
                                "parsed": {
                                    "info": {
                                        "tokenAmount": {
                                            "uiAmount": 15000.0
                                        }
                                    }
                                }
                            }
                        }
                    }
                ]
            }
        }
        mock_post.return_value = mock_res
        
        bal = get_creator_token_balance("some_mint", "creator_addr")
        self.assertEqual(bal, 15000.0)

    @patch("requests.post")
    def test_get_mint_security_info(self, mock_post):
        from tracker.helius import get_mint_security_info
        
        mock_res = MagicMock()
        mock_res.json.return_value = {
            "result": {
                "value": [
                    {
                        "data": {
                            "program": "spl-token",
                            "parsed": {
                                "info": {
                                    "mintAuthority": None,
                                    "freezeAuthority": "freeze_addr"
                                }
                            }
                        }
                    }
                ]
            }
        }
        mock_post.return_value = mock_res
        
        sec = get_mint_security_info("some_mint")
        self.assertIsNotNone(sec)
        self.assertIsNone(sec["mint_authority"])
        self.assertEqual(sec["freeze_authority"], "freeze_addr")

    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_alert_formats_security_and_holders_info(self, mock_get_allowed, mock_send_msg):
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
            logo_url="", # empty so it goes to sendMessage
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

        security_info = {
            "holders_dist": {
                "total_supply": 1000000.0,
                "top_10_percent": 82.5,  # Above 70% threshold
                "top_20_percent": 90.0,
                "holders": []
            },
            "creator_balance": 150000.0,  # 15% - Above 5% threshold
            "mint_security": {
                "mint_authority": "some_auth",  # Enabled
                "freeze_authority": None        # Revoked
            }
        }

        # Send alert with security_info
        success = send_alert(alert, security_info=security_info)

        self.assertTrue(success)
        mock_send_msg.assert_called_once()
        
        # Verify call args contain the formatted text
        text_arg = mock_send_msg.call_args[0][1]
        self.assertIn("Holder Distribution:", text_arg)
        self.assertIn("Top 10 hold:</b> 82.5% ⚠️ (High concentration)", text_arg)
        self.assertIn("Developer:</b> 15.0% (150.0K tokens) ⚠️ (High dev holding)", text_arg)
        self.assertIn("Security Checks:", text_arg)
        self.assertIn("Mint Authority:</b> ⚠️ Enabled (Dev can mint!)", text_arg)
        self.assertIn("Freeze Authority:</b> ✅ Revoked (Cannot freeze)", text_arg)

    @patch("tracker.telegram_bot.send_coordinated_alert")
    @patch("tracker.ai.get_token_risk")
    @patch("tracker.helius.get_token_creator", return_value="creator_addr")
    def test_coordinated_buy_alert_deduplication(self, mock_creator, mock_get_risk, mock_send_coordinated):
        from django.core.cache import cache
        cache.clear()

        # Prepare two wallets
        wallet1 = self.wallet
        wallet2 = Wallet.objects.create(
            address="TestWalletAddress2222222222222222222222222",
            nickname="Test_Wallet_2",
            added_by_telegram_id=12345,
        )

        mock_get_risk.return_value = {
            "level": "MEDIUM",
            "reason": "good liquidity",
            "dex_data": {
                "liquidity_usd": 10000,
            }
        }

        # Create first past buy from wallet 1
        TokenBuy.objects.create(
            wallet=wallet1,
            name="Gato Godxi",
            symbol="Godxi",
            contract_address="GatoContract1111111111111111111",
            timestamp=self.now - timedelta(minutes=10),
            amount_spent=Decimal("0.5"),
            amount=Decimal("50.0"),
            spent_symbol="SOL",
        )

        # Mock payload 1: wallet 2 buying the same token
        payload1 = {
            "type": "SWAP",
            "signature": "sig_coord_1",
            "timestamp": int(self.now.timestamp()),
            "tokenTransfers": [
                {
                    "toUserAccount": wallet2.address,
                    "mint": "GatoContract1111111111111111111",
                    "tokenAmount": 100.0,
                    "tokenName": "Gato Godxi",
                    "tokenSymbol": "Godxi",
                    "tokenIcon": "http://logo.url"
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": wallet2.address,
                    "amount": 1000000000
                }
            ]
        }

        # Run task process for first buy -> should trigger coordinated buy alert
        process_buy_event(payload1)
        mock_send_coordinated.assert_called_once()
        mock_send_coordinated.reset_mock()

        # Mock payload 2: wallet 2 buying again (different transaction but same buyer count = 2)
        payload2 = {
            "type": "SWAP",
            "signature": "sig_coord_2",
            "timestamp": int(self.now.timestamp()) + 5,
            "tokenTransfers": [
                {
                    "toUserAccount": wallet2.address,
                    "mint": "GatoContract1111111111111111111",
                    "tokenAmount": 50.0,
                    "tokenName": "Gato Godxi",
                    "tokenSymbol": "Godxi",
                    "tokenIcon": "http://logo.url"
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": wallet2.address,
                    "amount": 500000000
                }
            ]
        }

        # Run task process for second buy -> should NOT trigger coordinated buy alert again since count of unique wallets is still 2
        process_buy_event(payload2)
        mock_send_coordinated.assert_not_called()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_coordinated_alert_uses_photo_when_logo_present(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        from tracker.telegram_bot import send_coordinated_alert
        
        buy = TokenBuy.objects.create(
            wallet=self.wallet,
            name="Kori The Pom",
            symbol="KORI",
            contract_address="kori_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
            logo_url="http://example.com/kori.png",
        )
        
        mock_send_photo.return_value = True
        
        success = send_coordinated_alert("kori_addr", [buy])
        
        self.assertTrue(success)
        mock_send_photo.assert_called_once()
        mock_send_msg.assert_not_called()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_coordinated_alert_falls_back_on_photo_failure(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        from tracker.telegram_bot import send_coordinated_alert
        
        buy = TokenBuy.objects.create(
            wallet=self.wallet,
            name="Kori The Pom",
            symbol="KORI",
            contract_address="kori_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
            logo_url="http://example.com/kori.png",
        )
        
        mock_send_photo.return_value = False
        mock_send_msg.return_value = True
        
        success = send_coordinated_alert("kori_addr", [buy])
        
        self.assertTrue(success)
        mock_send_photo.assert_called_once()
        mock_send_msg.assert_called_once()

    @patch("tracker.telegram_bot._send_photo")
    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
    def test_send_coordinated_alert_no_photo_when_logo_missing(self, mock_get_allowed, mock_send_msg, mock_send_photo):
        from tracker.telegram_bot import send_coordinated_alert
        
        buy = TokenBuy.objects.create(
            wallet=self.wallet,
            name="Kori The Pom",
            symbol="KORI",
            contract_address="kori_addr",
            timestamp=self.now,
            amount_spent=Decimal("1.0"),
            amount=Decimal("100.0"),
            spent_symbol="SOL",
            logo_url="", # empty
        )
        
        mock_send_msg.return_value = True
        
        success = send_coordinated_alert("kori_addr", [buy])
        
        self.assertTrue(success)
        mock_send_photo.assert_not_called()
        mock_send_msg.assert_called_once()

    @patch("tracker.helius.get_token_metadata")
    @patch("tracker.helius.get_token_creator", return_value="creator_addr")
    @patch("tracker.telegram_bot.send_alert")
    def test_process_buy_event_queries_metadata_on_missing_logo_url(self, mock_send_alert, mock_get_creator, mock_get_metadata):
        # Setup mock return value for metadata
        mock_get_metadata.return_value = {
            "name": "Kori The Pom",
            "symbol": "KORI",
            "logo_url": "http://example.com/logo.png"
        }

        # Payload has tokenName and tokenSymbol, but NO tokenIcon (resembling real webhooks)
        payload = {
            "type": "SWAP",
            "signature": "sig_missing_logo",
            "timestamp": int(self.now.timestamp()),
            "tokenTransfers": [
                {
                    "toUserAccount": self.wallet.address,
                    "mint": "kori_addr",
                    "tokenAmount": 100.0,
                    "tokenName": "Kori The Pom",
                    "tokenSymbol": "KORI",
                }
            ],
            "nativeTransfers": [
                {
                    "fromUserAccount": self.wallet.address,
                    "amount": 1000000000
                }
            ]
        }

        process_buy_event(payload)

        # Assert that get_token_metadata was called to fetch the missing logo
        mock_get_metadata.assert_called_once_with("kori_addr")
        
        # Verify it was saved to the database with the resolved logo URL
        buy = TokenBuy.objects.get(contract_address="kori_addr")
        self.assertEqual(buy.logo_url, "http://example.com/logo.png")
