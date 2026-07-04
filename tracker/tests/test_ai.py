from unittest import TestCase
from unittest.mock import patch
from tracker.ai import understand_message

class AIParsingTests(TestCase):
    @patch("tracker.ai._call_ai")
    def test_understand_message_clean_actions(self, mock_call_ai):
        # Scenario 1: Model returned with brackets and quotes
        mock_call_ai.return_value = "ACTION: ['list_wallets']"
        res = understand_message("list wallets", [], [], 0, 0)
        self.assertEqual(res, {"type": "action", "action": "list_wallets"})

        # Scenario 2: Model returned with double quotes and brackets
        mock_call_ai.return_value = 'ACTION: ["add_wallet"]\nADDRESS: ["Solana123"]\nNICKNAME: ["Testy"]'
        res = understand_message("add wallet Solana123 Testy", [], [], 0, 0)
        self.assertEqual(res, {
            "type": "action",
            "action": "add_wallet",
            "address": "Solana123",
            "nickname": "Testy"
        })

        # Scenario 3: Model returned with parentheses and single quotes
        mock_call_ai.return_value = "ACTION: ('remove_wallet')\nNICKNAME: ('Shamo')"
        res = understand_message("remove Shamo", [], [], 0, 0)
        self.assertEqual(res, {
            "type": "action",
            "action": "remove_wallet",
            "nickname": "Shamo"
        })
