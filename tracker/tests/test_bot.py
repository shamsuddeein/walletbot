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
    @patch("tracker.ai.understand_message")
    async def test_add_wallet_triggers_confirmation(self, mock_understand_message, mock_cmd_remove, mock_cmd_add):
        # Mock the AI's decision to add a wallet
        mock_understand_message.return_value = {
            "type": "action",
            "action": "add_wallet",
            "address": "SolanaAddress123",
            "nickname": "Testy"
        }

        self.update.message.text = "Add wallet SolanaAddress123 as Testy"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify it did not call cmd_add_wallet immediately
        mock_cmd_add.assert_not_called()
        
        # Verify it stored the pending action
        self.assertEqual(
            self.context.user_data.get("pending_action"),
            {"action": "add_wallet", "address": "SolanaAddress123", "nickname": "Testy"}
        )
        
        # Verify user was prompted for confirmation
        self.update.message.reply_text.assert_any_call(
            "I understood: add the wallet named Testy with address SolanaAddress123. Reply yes to confirm, or no to cancel.",
            parse_mode=""
        )

    @patch("tracker.telegram_bot.cmd_add_wallet", new_callable=AsyncMock)
    @patch("tracker.telegram_bot.cmd_remove_wallet", new_callable=AsyncMock)
    async def test_confirm_yes_executes_action(self, mock_cmd_remove, mock_cmd_add):
        self.context.user_data["pending_action"] = {
            "action": "add_wallet",
            "address": "SolanaAddress123",
            "nickname": "Testy"
        }
        
        self.update.message.text = "yes"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify cmd_add_wallet was executed with the correct args
        mock_cmd_add.assert_called_once_with(self.update, self.context)
        self.assertEqual(self.context.args, ["SolanaAddress123", "Testy"])
        
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
    @patch("tracker.ai.understand_message")
    async def test_invalid_input_reprompts(self, mock_understand_message, mock_cmd_remove, mock_cmd_add):
        self.context.user_data["pending_action"] = {
            "action": "remove_wallet",
            "nickname": "Shamo"
        }
        
        self.update.message.text = "hello bot"
        
        await cmd_natural_language(self.update, self.context)
        
        # Verify no commands executed and no NLP invoked
        mock_cmd_add.assert_not_called()
        mock_cmd_remove.assert_not_called()
        mock_understand_message.assert_not_called()
        
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
