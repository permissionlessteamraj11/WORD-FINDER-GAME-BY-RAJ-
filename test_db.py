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
        self.assertEqual(data[0][0], "Alice")
        self.assertEqual(data[0][1], 30)
        self.assertEqual(data[1][0], "Bob")
        self.assertEqual(data[1][1], 25)

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
        self.assertEqual(data_daily_after[0][1], 15)  # 0 reset + 15
        self.assertEqual(data_daily_after[0][2], 1)   # 0 reset + 1 win

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

if __name__ == "__main__":
    unittest.main()
