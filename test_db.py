import unittest
import os
import json
import time
from datetime import datetime, timedelta, timezone
from main import JSONDatabaseManager, create_application

class TestJSONDatabaseManager(unittest.TestCase):
    def setUp(self):
        self.test_db_path = "test_database.json"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = JSONDatabaseManager(self.test_db_path)

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        temp_file = f"{self.test_db_path}.tmp"
        if os.path.exists(temp_file):
            os.remove(temp_file)

    def test_init_and_file_creation(self):
        self.assertTrue(os.path.exists(self.test_db_path))
        with open(self.test_db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("leaderboard", data)
        self.assertIn("active_games", data)
        self.assertIn("known_chats", data)

    def test_game_state_crud(self):
        chat_id = -100123456
        state = {"word": "APPLE", "difficulty": "medium", "guesses": ["GUESS"]}

        # Get when empty
        self.assertIsNone(self.db.get_game_state(chat_id))

        # Save
        self.db.save_game_state(chat_id, state)
        saved = self.db.get_game_state(chat_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["chat_id"], chat_id)
        self.assertEqual(saved["word"], "APPLE")

        # Delete
        self.db.delete_game_state(chat_id)
        self.assertIsNone(self.db.get_game_state(chat_id))

    def test_known_chats(self):
        chat_id = -100999888
        self.db.add_chat(chat_id, "SUPERGROUP", time.time())
        all_chats = self.db.get_all_chat_ids()
        self.assertIn(chat_id, all_chats)

    def test_leaderboard_tracking_and_sorting(self):
        self.db.update_leaderboard(1, "Alice", 10)
        self.db.update_leaderboard(2, "Bob", 25)
        self.db.update_leaderboard(1, "Alice", 20)  # Total 30 for Alice

        data = self.db.get_leaderboard_data("global")
        self.assertEqual(len(data), 2)
        # Alice has 30 points, Bob has 25 points. Alice should be first.
        self.assertEqual(data[0][1], "Alice")
        self.assertEqual(data[0][2], 30)
        self.assertEqual(data[1][1], "Bob")
        self.assertEqual(data[1][2], 25)

    def test_leaderboard_period_reset(self):
        user_id = 100
        now = datetime.now(timezone.utc)
        old_win_date = (now - timedelta(days=2)).isoformat()

        # Manually seed an old record for user_id in daily stats
        self.db.data["leaderboard"][str(user_id)] = {
            "user_id": user_id,
            "username": "OldPlayer",
            "points_global": 100,
            "wins_global": 5,
            "points_daily": 50,
            "wins_daily": 2,
            "last_win_date_daily": old_win_date,
            "points_weekly": 50,
            "wins_weekly": 2,
            "last_win_date_weekly": old_win_date
        }
        self.db._save()

        # Fetch daily leaderboard - old daily points should be reset to 0
        data_daily = self.db.get_leaderboard_data("daily")
        self.assertEqual(len(data_daily), 0)  # points_daily is 0, so excluded from daily leaderboard

        # Now update leaderboard for OldPlayer
        self.db.update_leaderboard(user_id, "OldPlayer", 15)

        data_daily_after = self.db.get_leaderboard_data("daily")
        self.assertEqual(len(data_daily_after), 1)
        self.assertEqual(data_daily_after[0][2], 15)  # 0 reset + 15
        self.assertEqual(data_daily_after[0][3], 1)   # 0 reset + 1 win

    def test_seek_auth_crud_and_backwards_compatibility(self):
        chat_id = -100123456
        user_id = 999
        self.assertFalse(self.db.has_seek_auth(chat_id, user_id))
        self.assertFalse(self.db.has_seek_oauth(chat_id, user_id))

        self.db.grant_seek_auth(chat_id, user_id)
        self.assertTrue(self.db.has_seek_auth(chat_id, user_id))
        self.assertTrue(self.db.has_seek_oauth(chat_id, user_id))

        self.db.revoke_seek_auth(chat_id, user_id)
        self.assertFalse(self.db.has_seek_auth(chat_id, user_id))
        self.assertFalse(self.db.has_seek_oauth(chat_id, user_id))

        self.db.grant_seek_oauth(chat_id, user_id)
        self.assertTrue(self.db.has_seek_auth(chat_id, user_id))

        self.db.revoke_seek_oauth(chat_id, user_id)
        self.assertFalse(self.db.has_seek_auth(chat_id, user_id))

    def test_seek_oauth_migration(self):
        migration_db_path = "test_migration_db.json"
        if os.path.exists(migration_db_path):
            os.remove(migration_db_path)
        try:
            with open(migration_db_path, "w", encoding="utf-8") as f:
                json.dump({"seek_oauth": {"-100555": [777]}}, f)
            migrated_db = JSONDatabaseManager(migration_db_path)
            self.assertTrue(migrated_db.has_seek_auth(-100555, 777))
        finally:
            if os.path.exists(migration_db_path):
                os.remove(migration_db_path)

class TestApplicationConfiguration(unittest.TestCase):
    def test_create_application_custom_timeouts(self):
        token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        app = create_application(token)
        self.assertIsNotNone(app)

        # Check HTTPXRequest timeouts on bot
        request = app.bot.request
        timeout = request._client_kwargs.get("timeout")
        self.assertIsNotNone(timeout)
        self.assertEqual(timeout.connect, 30.0)
        self.assertEqual(timeout.read, 30.0)
        self.assertEqual(timeout.write, 30.0)

    def test_create_application_none_token(self):
        app = create_application(None)
        self.assertIsNone(app)



class TestSafeEditMessage(unittest.IsolatedAsyncioTestCase):
    async def test_text_message_edit_success(self):
        from unittest.mock import AsyncMock, MagicMock
        from main import safe_edit_message
        query = MagicMock(spec=['message', 'edit_message_text'])
        query.message = MagicMock(spec=['text'])
        query.edit_message_text = AsyncMock()

        await safe_edit_message(query, "Hello World")
        query.edit_message_text.assert_called_once_with(text="Hello World", parse_mode="HTML")

    async def test_photo_message_edit_caption(self):
        from unittest.mock import AsyncMock, MagicMock
        from main import safe_edit_message
        query = MagicMock(spec=['message', 'edit_message_caption', 'edit_message_text'])
        query.message = MagicMock()
        query.message.photo = ["fake_photo"]
        query.edit_message_caption = AsyncMock()

        await safe_edit_message(query, "New Caption")
        query.edit_message_caption.assert_called_once_with(caption="New Caption", parse_mode="HTML")

    async def test_bad_request_no_text_fallback_to_caption(self):
        from unittest.mock import AsyncMock, MagicMock
        from telegram import error
        from main import safe_edit_message
        query = MagicMock(spec=['message', 'edit_message_text', 'edit_message_caption'])
        query.message = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=error.BadRequest("There is no text in the message to edit"))
        query.edit_message_caption = AsyncMock()

        await safe_edit_message(query, "Fallback Text")
        query.edit_message_caption.assert_called_once_with(caption="Fallback Text", parse_mode="HTML")

    async def test_message_not_modified_ignored(self):
        from unittest.mock import AsyncMock, MagicMock
        from telegram import error
        from main import safe_edit_message
        query = MagicMock(spec=['message', 'edit_message_text'])
        query.message = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=error.BadRequest("Message is not modified: specified new message content and reply markup are exactly the same"))

        await safe_edit_message(query, "Same Text")

    async def test_fallback_to_reply_text_on_failure(self):
        from unittest.mock import AsyncMock, MagicMock
        from telegram import error
        from main import safe_edit_message
        query = MagicMock(spec=['message', 'edit_message_text', 'edit_message_caption'])
        query.message = MagicMock()
        query.message.reply_text = AsyncMock()
        query.edit_message_text = AsyncMock(side_effect=error.BadRequest("Unknown error"))
        query.edit_message_caption = AsyncMock(side_effect=error.BadRequest("Unknown error"))

        await safe_edit_message(query, "Fallback Reply")
        query.message.reply_text.assert_called_once_with(text="Fallback Reply", parse_mode="HTML")


class TestSeekAuthCommands(unittest.IsolatedAsyncioTestCase):
    async def test_seekauth_and_rmseekauth_commands(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from telegram.constants import ChatType
        import main
        from main import seekauth_command, rmseekauth_command

        test_db_path = "test_cmd_db.json"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        db = JSONDatabaseManager(test_db_path)
        main.db_manager = db

        try:
            # Mock update and context
            update = MagicMock()
            update.effective_chat.type = ChatType.SUPERGROUP
            update.effective_chat.id = -100111222
            update.effective_user.id = 12345
            update.effective_message.reply_text = AsyncMock()

            # Mock target user reply
            target_user = MagicMock()
            target_user.id = 67890
            target_user.username = "targetuser"
            update.effective_message.reply_to_message.from_user = target_user

            context = MagicMock()

            with patch('main.is_group_admin', AsyncMock(return_value=True)):
                await seekauth_command(update, context)
                self.assertTrue(db.has_seek_auth(-100111222, 67890))
                update.effective_message.reply_text.assert_called_with("✅ Granted game end power to @targetuser in this group.")

                await rmseekauth_command(update, context)
                self.assertFalse(db.has_seek_auth(-100111222, 67890))
                update.effective_message.reply_text.assert_called_with("✅ Revoked game end power from @targetuser in this group.")

            # Test private chat restriction
            update.effective_chat.type = ChatType.PRIVATE
            await seekauth_command(update, context)
            update.effective_message.reply_text.assert_called_with("❌ This command can only be used in groups.")

            # Test seekauth via numeric user ID and username
            db.save_user(998877, 'saveduser', 'Saved User')

            # Setup update without reply
            update.effective_chat.type = ChatType.SUPERGROUP
            update.effective_message.reply_to_message = None
            update.effective_message.entities = []

            # Test user ID
            context.args = ['998877']
            with patch('main.is_group_admin', AsyncMock(return_value=True)):
                await seekauth_command(update, context)
                self.assertTrue(db.has_seek_auth(-100111222, 998877))

                await rmseekauth_command(update, context)
                self.assertFalse(db.has_seek_auth(-100111222, 998877))

            # Test username argument
            context.args = ['@saveduser']
            with patch('main.is_group_admin', AsyncMock(return_value=True)):
                await seekauth_command(update, context)
                self.assertTrue(db.has_seek_auth(-100111222, 998877))

                await rmseekauth_command(update, context)
                self.assertFalse(db.has_seek_auth(-100111222, 998877))
        finally:
            main.db_manager = None
            if os.path.exists(test_db_path):
                os.remove(test_db_path)

    async def test_end_game_with_seekauth_permissions(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from telegram.constants import ChatType
        import main
        from main import end_game_command

        test_db_path = "test_cmd_db2.json"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        db = JSONDatabaseManager(test_db_path)
        main.db_manager = db

        try:
            chat_id = -100999888
            db.save_game_state(chat_id, {'word': 'TESTS', 'difficulty': 'medium', 'guesses_made': 0, 'max_guesses': 10})

            update = MagicMock()
            update.effective_chat.id = chat_id
            update.effective_user.id = 554433
            update.effective_user.username = "normaluser"
            update.effective_user.first_name = "Normal"
            update.effective_message.reply_text = AsyncMock()

            context = MagicMock()

            # Case 1: Non-admin, no seekauth -> Permission denied
            with patch('main.is_group_admin', AsyncMock(return_value=False)):
                await end_game_command(update, context)
                self.assertIsNotNone(db.get_game_state(chat_id))
                update.effective_message.reply_text.assert_called_with("🚨 You don't have permission to end the game. Group admins or authorized members (/seekauth) can end it.", parse_mode='Markdown')

            # Case 2: Grant seekauth -> Normal user can end game
            db.grant_seek_auth(chat_id, 554433)
            with patch('main.is_group_admin', AsyncMock(return_value=False)):
                await end_game_command(update, context)
                self.assertIsNone(db.get_game_state(chat_id))
        finally:
            main.db_manager = None
            if os.path.exists(test_db_path):
                os.remove(test_db_path)

if __name__ == "__main__":
    unittest.main()
