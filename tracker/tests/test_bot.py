from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch
from tracker.telegram_bot import cmd_natural_language

class BotConfirmationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        # Start patcher for owner access control
        self.patcher_allowed = patch("tracker.telegram_bot._get_allowed_user_id", return_value=12345)
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
