import unittest
import asyncio
import os
import json
import html
from unittest.mock import AsyncMock, MagicMock
from main import START_MESSAGE, start_new_game_logic, JSONDatabaseManager, process_guess_logic, handle_guess

class TestGameMessagesFormatting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_db_path = "test_game_messages_db.json"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        import main
        main.db_manager = JSONDatabaseManager(self.test_db_path)

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        temp_file = f"{self.test_db_path}.tmp"
        if os.path.exists(temp_file):
            os.remove(temp_file)

    def test_start_message_blockquote(self):
        # Requirement 1: ONLY the Quick Start section must appear inside a proper Telegram blockquote.
        # The welcome text above it must remain outside the quote.
        # The final sentence below it must remain outside the quote.
        self.assertTrue(START_MESSAGE.startswith("✨ Welcome to WordSeek!!"))
        self.assertIn("<blockquote>🚀 Quick Start:\n• Use /new to start a new game\n• Add me to a group with admin permissions to play with friends\n• Use /help for detailed instructions and command list</blockquote>", START_MESSAGE)
        self.assertTrue(START_MESSAGE.endswith("Ready to test your word skills? Let's play!"))

    async def test_new_game_message_blockquote(self):
        # Requirement 2: New game introduction/status inside blockquote
        chat_id = 12345
        success, message = await start_new_game_logic(chat_id, "medium")
        self.assertTrue(success)
        self.assertTrue(message.startswith("<blockquote>"))
        self.assertTrue(message.endswith("</blockquote>"))
        self.assertIn("<b>✨ New Word Rush Challenge!</b>", message)
        self.assertIn("🎯 Difficulty: <b>Medium</b>", message)
        self.assertIn("📜 Word Length: <b>5 letters</b>", message)

    async def test_winning_message_blockquote(self):
        # Requirement 4: Winning message inside blockquote
        import main
        chat_id = 999
        main.db_manager.save_game_state(chat_id, {
            'word': 'APPLE',
            'difficulty': 'medium',
            'guesses_made': 0,
            'max_guesses': 30,
            'guess_history': [],
            'guessed_words': []
        })

        update = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_user.id = 777
        update.effective_user.username = "testuser"
        update.effective_user.first_name = "Test"
        update.effective_user.full_name = "Test User"
        update.message.text = "APPLE"
        update.message.reply_text = AsyncMock()
        update.message.set_reaction = AsyncMock()

        context = MagicMock()

        await handle_guess(update, context)

        update.message.reply_text.assert_called_once()
        call_args, call_kwargs = update.message.reply_text.call_args
        reply_text = call_args[0]
        parse_mode = call_kwargs.get("parse_mode")

        self.assertEqual(parse_mode, "HTML")
        self.assertTrue(reply_text.startswith("<blockquote>"))
        self.assertTrue(reply_text.endswith("</blockquote>"))
        self.assertIn("🏆 <b>Congrats! You guessed it correctly.</b>", reply_text)
        self.assertIn("👤 <b>Winner:</b> @testuser", reply_text)
        self.assertIn("🔑 <b>Correct Word:</b> <code>apple</code>", reply_text)

    async def test_losing_message_blockquote(self):
        # Requirement 3: Game ending/loss message inside blockquote
        import main
        chat_id = 888
        main.db_manager.save_game_state(chat_id, {
            'word': 'APPLE',
            'difficulty': 'medium',
            'guesses_made': 29,
            'max_guesses': 30,
            'guess_history': [" <code>🟥 🟥 🟥 🟥 🟥</code> - <b>OTHER</b>"] * 29,
            'guessed_words': ["OTHER"] * 29
        })

        update = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_user.id = 777
        update.effective_user.username = "testuser"
        update.effective_user.first_name = "Test"
        update.message.text = "WRONG"
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        await handle_guess(update, context)

        update.message.reply_text.assert_called_once()
        call_args, call_kwargs = update.message.reply_text.call_args
        reply_text = call_args[0]
        parse_mode = call_kwargs.get("parse_mode")

        self.assertEqual(parse_mode, "HTML")
        self.assertTrue(reply_text.startswith("<blockquote>"))
        self.assertTrue(reply_text.endswith("</blockquote>"))
        self.assertIn("💔 <b>GAME OVER! 😭</b>", reply_text)
        self.assertIn("❌ <i>The secret word was:</i> <b><code>APPLE</code></b>", reply_text)

if __name__ == "__main__":
    unittest.main()
