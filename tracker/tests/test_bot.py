from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch
from tracker.telegram_bot import cmd_natural_language

class BotConfirmationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.patcher_allowed = patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
        self.mock_allowed = self.patcher_allowed.start()
        
        self.update = MagicMock()
        self.update.effective_user.id = 12345
        self.update.message = AsyncMock()
        self.update.message.text = ""
        self.update.message.caption = None
        
        self.context = MagicMock()
        self.context.user_data = {}
        self.context.args = []

    def tearDown(self):
        self.patcher_allowed.stop()

    @patch("tracker.telegram_bot.cmd_add_wallet", new_callable=AsyncMock)
    @patch("tracker.telegram_bot.cmd_remove_wallet", new_callable=AsyncMock)
    async def test_add_wallet_triggers_confirmation(self, mock_cmd_remove, mock_cmd_add):
        self.update.message.text = "add wallet 6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY Testy"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify it did not call cmd_add_wallet immediately
        mock_cmd_add.assert_not_called()
        
        # Verify it stored the pending action
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {
                "action": "add_wallet",
                "address": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
                "nickname": "Testy"
            }
        )
        
        # Verify user was prompted for confirmation
        self.update.message.reply_text.assert_any_call(
            "I understood: add the wallet named Testy with address 6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )

    @patch("tracker.telegram_bot.cmd_add_wallet", new_callable=AsyncMock)
    @patch("tracker.telegram_bot.cmd_remove_wallet", new_callable=AsyncMock)
    async def test_confirm_yes_executes_action(self, mock_cmd_remove, mock_cmd_add):
        self.context.user_data["pending_action"] = {
            "action": "add_wallet",
            "address": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
            "nickname": "Testy"
        }
        
        self.update.message.text = "yes"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify cmd_add_wallet was executed with the correct args
        mock_cmd_add.assert_called_once_with(self.update, self.context)
        self.assertEqual(self.context.args, ["6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY", "Testy"])
        
        # Verify pending_action is cleared
        self.assertNotIn("pending_action", self.context.user_data)

    @patch("tracker.telegram_bot.cmd_add_wallet", new_callable=AsyncMock)
    @patch("tracker.telegram_bot.cmd_remove_wallet", new_callable=AsyncMock)
    async def test_confirm_no_cancels_action(self, mock_cmd_remove, mock_cmd_add):
        self.context.user_data["pending_action"] = {
            "action": "remove_wallet",
            "nickname": "Shamo"
        }
        
        self.update.message.text = "no"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify neither command was executed
        mock_cmd_add.assert_not_called()
        mock_cmd_remove.assert_not_called()
        
        # Verify it replied "Okay, cancelled."
        self.update.message.reply_text.assert_called_once_with("Okay, cancelled.", parse_mode="")
        
        # Verify pending_action is cleared
        self.assertNotIn("pending_action", self.context.user_data)

    @patch("tracker.telegram_bot.cmd_add_wallet", new_callable=AsyncMock)
    @patch("tracker.telegram_bot.cmd_remove_wallet", new_callable=AsyncMock)
    async def test_invalid_input_reprompts(self, mock_cmd_remove, mock_cmd_add):
        self.context.user_data["pending_action"] = {
            "action": "remove_wallet",
            "nickname": "Shamo"
        }
        
        self.update.message.text = "hello bot"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify no commands executed
        mock_cmd_add.assert_not_called()
        mock_cmd_remove.assert_not_called()
        
        # Verify it re-prompts the confirmation
        self.update.message.reply_text.assert_called_once_with(
            "I understood: remove the wallet named Shamo. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )
        
        # Verify pending_action remains in user_data
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {"action": "remove_wallet", "nickname": "Shamo"}
        )

    @patch("tracker.telegram_bot.cmd_list_wallets", new_callable=AsyncMock)
    async def test_list_wallets_command(self, mock_cmd_list):
        self.update.message.text = "lists"
        await cmd_natural_language(self.update, self.context)
        mock_cmd_list.assert_called_once_with(self.update, self.context)

    async def test_multistep_add_wallet(self):
        # Step 1: Send just an address
        self.update.message.text = "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY"
        await cmd_natural_language(self.update, self.context)
        
        # Verify address is pending
        self.assertEqual(
            self.context.user_data.get("pending_add_address"),
            "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY"
        )
        self.update.message.reply_text.assert_called_once_with(
            "Great! I have the address. What nickname would you like to assign to this wallet?",
            parse_mode=""
        )
        
        # Step 2: Send nickname
        self.update.message.text = "sol2"
        self.update.message.reply_text.reset_mock()
        await cmd_natural_language(self.update, self.context)
        
        # Verify pending address is cleared and confirmation gate is active
        self.assertNotIn("pending_add_address", self.context.user_data)
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {
                "action": "add_wallet",
                "address": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
                "nickname": "sol2"
            }
        )
        self.update.message.reply_text.assert_called_once_with(
            "I understood: add the wallet named sol2 with address 6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )

    @patch("tracker.telegram_bot.cmd_profile", new_callable=AsyncMock)
    async def test_callback_profile_triggers_command(self, mock_cmd_profile):
        from tracker.telegram_bot import handle_callback_query
        
        callback_query = MagicMock()
        callback_query.data = "profile_sol2"
        callback_query.answer = AsyncMock()
        self.update.callback_query = callback_query
        self.update.message = None
        
        await handle_callback_query(self.update, self.context)
        
        callback_query.answer.assert_called_once()
        mock_cmd_profile.assert_called_once_with(self.update, self.context)
        self.assertEqual(self.context.args, ["sol2"])
        self.assertEqual(self.update.message, callback_query.message)

    async def test_callback_remove_sets_pending_action(self):
        from tracker.telegram_bot import handle_callback_query
        
        callback_query = MagicMock()
        callback_query.data = "remove_sol2"
        callback_query.answer = AsyncMock()
        callback_query.message = AsyncMock()
        self.update.callback_query = callback_query
        self.update.message = None
        
        await handle_callback_query(self.update, self.context)
        
        callback_query.answer.assert_called_once()
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {"action": "remove_wallet", "nickname": "sol2"}
        )
        callback_query.message.reply_text.assert_called_once_with(
            "⚠️ You clicked Stop Tracking. Do you want to remove the wallet named sol2? Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )

    @patch("tracker.tasks.backfill_wallet_history_task.delay")
    @patch("tracker.helius.register_wallet", return_value=True)
    @patch("tracker.telegram_bot.db_add_wallet", return_value="ok")
    async def test_cmd_add_wallet_triggers_backfill(self, mock_db_add, mock_register, mock_backfill):
        from tracker.telegram_bot import cmd_add_wallet
        
        self.context.args = ["6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY", "my_new_wallet"]
        
        await cmd_add_wallet(self.update, self.context)
        
        # Verify Helius registration was called
        mock_register.assert_called_once_with("6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY")
        
        # Verify the Celery backfill task was triggered
        mock_backfill.assert_called_once_with(
            "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
            "my_new_wallet",
            12345
        )


from django.test import TransactionTestCase

class BackfillTaskTests(TransactionTestCase):
    def setUp(self):
        from tracker.models import Wallet
        self.wallet = Wallet.objects.create(
            address="6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
            nickname="trader_shamo",
            added_by_telegram_id=12345
        )

    @patch("tracker.telegram_bot._send_message")
    @patch("tracker.matching.compute_logo_hash", return_value="f0f0")
    @patch("tracker.helius.get_token_metadata")
    @patch("requests.get")
    def test_backfill_wallet_history_success(self, mock_get, mock_metadata, mock_logo_hash, mock_send_message):
        from tracker.tasks import backfill_wallet_history_task
        from tracker.models import TokenBuy
        import datetime
        from django.utils import timezone
        
        now_unix = int(timezone.now().timestamp())
        # Simulate two parsed swap transactions
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [
            {
                "signature": "sig1",
                "type": "SWAP",
                "timestamp": now_unix,
                "tokenTransfers": [
                    {
                        "toUserAccount": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
                        "mint": "Mint111111111111111111111111111111111111",
                        "tokenAmount": 100.0,
                        "tokenName": "Token A",
                        "tokenSymbol": "TKA",
                        "tokenIcon": "http://logo.url/a.png"
                    }
                ],
                "nativeTransfers": [
                    {
                        "fromUserAccount": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
                        "amount": 1000000000 # 1 SOL
                    }
                ]
            },
            {
                "signature": "sig2",
                "type": "SWAP",
                "timestamp": now_unix - 10000,
                "tokenTransfers": [
                    {
                        "toUserAccount": "6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY",
                        "mint": "Mint222222222222222222222222222222222222",
                        "tokenAmount": 50.0,
                        "tokenName": "", # missing metadata
                        "tokenSymbol": "",
                        "tokenIcon": ""
                    }
                ]
            }
        ]
        
        mock_metadata.return_value = {
            "name": "Token B",
            "symbol": "TKB",
            "logo_url": "http://logo.url/b.png"
        }
        
        # Execute backfill
        backfill_wallet_history_task("6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY", "trader_shamo", 12345)
        
        # Verify TokenBuy records were saved
        self.assertEqual(TokenBuy.objects.filter(wallet=self.wallet).count(), 2)
        
        # Verify first buy details
        buy1 = TokenBuy.objects.get(tx_signature="sig1")
        self.assertEqual(buy1.name, "Token A")
        self.assertEqual(buy1.symbol, "TKA")
        self.assertEqual(buy1.amount, 100.0)
        self.assertEqual(buy1.amount_spent, 1.0)
        self.assertEqual(buy1.logo_hash, "f0f0")
        
        # Verify second buy details (fetched metadata fallback)
        buy2 = TokenBuy.objects.get(tx_signature="sig2")
        self.assertEqual(buy2.name, "Token B")
        self.assertEqual(buy2.symbol, "TKB")
        
        # Verify final Telegram message
        mock_send_message.assert_called_once()
        self.assertIn("loaded this wallet's last", mock_send_message.call_args[0][1])


class RemoveTwoStepTests(IsolatedAsyncioTestCase):
    """
    Tests for the /remove two-step flow:
    user types /remove with no args → bot asks for nickname → next message is
    treated as the wallet to remove (not as an add command).
    """
    def setUp(self):
        self.patcher_allowed = patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345])
        self.mock_allowed = self.patcher_allowed.start()

        self.update = MagicMock()
        self.update.effective_user.id = 12345
        self.update.message = AsyncMock()
        self.update.message.text = ""
        self.update.message.caption = None

        self.context = MagicMock()
        self.context.user_data = {}
        self.context.args = []

    def tearDown(self):
        self.patcher_allowed.stop()

    async def test_remove_nickname_reply_creates_pending_action(self):
        """
        After /remove stores pending_remove_query, replying with a nickname like
        'Ai koll 6' should NOT fall through to the fallback handler. It must
        create a pending_action for remove_wallet instead.
        """
        # Simulate state after /remove was called with no args
        self.context.user_data["pending_remove_query"] = True
        self.update.message.text = "Ai koll 6"

        await cmd_natural_language(self.update, self.context)

        # pending_remove_query should be consumed
        self.assertNotIn("pending_remove_query", self.context.user_data)

        # A pending_action for remove_wallet should be set
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {"action": "remove_wallet", "nickname": "Ai koll 6"}
        )
        # Bot should ask for confirmation
        self.update.message.reply_text.assert_any_call(
            "I understood: remove the wallet named Ai koll 6. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )

    async def test_remove_address_reply_does_not_trigger_add(self):
        """
        After /remove stores pending_remove_query, replying with a raw Solana
        address must be treated as a remove request — NOT as an add request.
        """
        self.context.user_data["pending_remove_query"] = True
        addr = "52oc72vjNbpUhF7jNE1pPAvc17JwBTyxybFp3u7PvetG"
        self.update.message.text = addr

        await cmd_natural_language(self.update, self.context)

        # pending_remove_query should be consumed
        self.assertNotIn("pending_remove_query", self.context.user_data)

        # pending_add_address must NOT have been set
        self.assertNotIn("pending_add_address", self.context.user_data)

        # A pending_action for remove_wallet should be set with the address
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {"action": "remove_wallet", "nickname": addr}
        )
        self.update.message.reply_text.assert_any_call(
            f"I understood: remove the wallet named {addr}. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )


class CallbackAccessControlTests(IsolatedAsyncioTestCase):
    """
    Tests for multiple allowed user access control in handle_callback_query.
    """
    def setUp(self):
        # Configure multiple allowed user IDs (e.g. 12345 as owner, 67890 as observer)
        self.patcher_allowed = patch("tracker.telegram_bot._get_allowed_user_ids", return_value=[12345, 67890])
        self.mock_allowed = self.patcher_allowed.start()

        self.update = MagicMock()
        self.update.message = None
        
        self.callback_query = MagicMock()
        self.callback_query.data = "profile_sol2"
        self.callback_query.answer = AsyncMock()
        self.callback_query.message = AsyncMock()
        self.update.callback_query = self.callback_query

        self.context = MagicMock()
        self.context.user_data = {}
        self.context.args = []

    def tearDown(self):
        self.patcher_allowed.stop()

    @patch("tracker.telegram_bot.cmd_profile", new_callable=AsyncMock)
    async def test_owner_can_trigger_callback(self, mock_cmd_profile):
        from tracker.telegram_bot import handle_callback_query
        self.update.effective_user.id = 12345  # Owner ID
        
        await handle_callback_query(self.update, self.context)
        
        self.callback_query.answer.assert_called_once()
        mock_cmd_profile.assert_called_once()
        self.callback_query.message.reply_text.assert_not_called()

    @patch("tracker.telegram_bot.cmd_profile", new_callable=AsyncMock)
    async def test_observer_can_trigger_callback(self, mock_cmd_profile):
        from tracker.telegram_bot import handle_callback_query
        self.update.effective_user.id = 67890  # Observer ID
        
        await handle_callback_query(self.update, self.context)
        
        self.callback_query.answer.assert_called_once()
        mock_cmd_profile.assert_called_once()
        self.callback_query.message.reply_text.assert_not_called()

    @patch("tracker.telegram_bot.cmd_profile", new_callable=AsyncMock)
    async def test_unauthorized_user_is_blocked_from_callback(self, mock_cmd_profile):
        from tracker.telegram_bot import handle_callback_query
        self.update.effective_user.id = 99999  # Unauthorized ID
        
        await handle_callback_query(self.update, self.context)
        
        # callback query answer should NOT be called if unauthorized or handled at decorators/checks
        # actually, the view block checks user_id and replies "You are not authorized to use this bot."
        self.callback_query.message.reply_text.assert_called_once_with(
            "You are not authorized to use this bot.",
            parse_mode=""
        )
        mock_cmd_profile.assert_not_called()


from django.test import TestCase

class PatternHistoryTests(TestCase):
    def setUp(self):
        from tracker.models import Wallet, TokenBuy, MatchAlert
        from django.utils import timezone as django_tz
        from datetime import timedelta
        
        self.wallet = Wallet.objects.create(
            address="TestWalletAddress111111111111111111111111",
            nickname="pattern_trader",
            added_by_telegram_id=12345
        )
        
        now = django_tz.now()
        
        # Past buy (similar)
        self.past_buy = TokenBuy.objects.create(
            wallet=self.wallet,
            name="CASHBULL",
            symbol="CASH",
            logo_url="http://logo.url/cash.png",
            logo_hash="f0f0",
            contract_address="MintCashBull11111111111111111111111111111",
            amount=100.0,
            timestamp=now - timedelta(days=2),
            tx_signature="sig_past",
            amount_spent=0.5,
            spent_symbol="SOL",
        )
        
        # New buy (trigger)
        self.new_buy = TokenBuy.objects.create(
            wallet=self.wallet,
            name="Bullcat",
            symbol="BCAT",
            logo_url="http://logo.url/cat.png",
            logo_hash="0f0f",
            contract_address="MintBullcat111111111111111111111111111111",
            amount=200.0,
            timestamp=now,
            tx_signature="sig_new",
            amount_spent=1.2,
            spent_symbol="SOL",
        )
        
        self.alert = MatchAlert.objects.create(
            new_buy=self.new_buy,
            matched_buy=self.past_buy,
            match_type="name",
            name_score=92.0,
            symbol_score=None,
            logo_distance=None,
        )

    def test_build_pattern_history_text(self):
        from tracker.telegram_bot import build_pattern_history_text
        text = build_pattern_history_text(self.wallet)
        
        self.assertIn("Similar Token History (showing top 15 of 1 pairs)", text)
        self.assertIn("<b>CASHBULL</b> (<b>CASH</b>) ➔ <b>Bullcat</b> (<b>BCAT</b>)", text)
        self.assertIn("2 days apart", text)
        self.assertIn("0.50 SOL ➔ 1.20 SOL", text)
        self.assertIn("name 92%", text)
        self.assertIn("By match type (last 100): name ×1", text)
        self.assertIn("Avg gap between similar buys (last 100): 2.0 days", text)

    def test_format_time_diff(self):
        from tracker.telegram_bot import format_time_diff
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        
        # Test days
        self.assertEqual(format_time_diff(now, now - timedelta(days=2)), "2 days")
        
        # Test hours
        self.assertEqual(format_time_diff(now, now - timedelta(hours=4)), "4 hours")
        
        # Test minutes (under 1 hour)
        self.assertEqual(format_time_diff(now, now - timedelta(minutes=35)), "35 minutes")
        
        # Test seconds (under 1 minute)
        self.assertEqual(format_time_diff(now, now - timedelta(seconds=15)), "15 seconds")
        
        # Test combination
        self.assertEqual(
            format_time_diff(now, now - timedelta(days=1, hours=3, minutes=10)),
            "1 day, 3 hours, 10 minutes"
        )


