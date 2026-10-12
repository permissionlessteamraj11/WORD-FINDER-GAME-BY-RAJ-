# WORD-FINDER-GAME-BY-RAJ-

WordRush is a Telegram word-guessing game bot built with `python-telegram-bot`.

## ⚙️ Setup & Environment Variables

Create a `.env` file in the root directory with the following variables:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_USER_ID=123456789  # Your Telegram User ID
LOG_CHANNEL_ID=-1003978131834  # Telegram log channel ID (optional)
JSON_DB_PATH=database.json  # Path to database file (optional)

# Network & Timeout settings (optional)
HTTP_CONNECT_TIMEOUT=30.0
HTTP_READ_TIMEOUT=30.0
HTTP_WRITE_TIMEOUT=30.0
HTTP_POOL_TIMEOUT=10.0
BOOTSTRAP_RETRIES=5
```

---

## 🔐 Admin Commands & Panel

To use Admin features in the bot:

1. **Set your Admin User ID:**
   Add your Telegram User ID in `.env` under `ADMIN_USER_ID`:
   ```env
   ADMIN_USER_ID=123456789
   ```

2. **Admin Commands:**
   - `/broadcast <message>`: Broadcast a message to all active chats where the bot is present (Bot Owner / Admin only).
   - `/end`: Force end an active game in a chat (Group Admins or Private DM).
   - `/difficulty`: View and manage game difficulty settings (Group Admins or Private DM).

---

## 🎮 General Player Commands

- `/start` or `/help`: Open the main menu and help center.
- `/new [difficulty]`: Start a new game (`easy`, `medium`, `hard`, `extreme`).
- `/status`: View current active game status and guess history.
- `/leaderboard [period]`: View rankings (`daily`, `weekly`, `monthly`, `global`).
