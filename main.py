import time
BOT_START_TIME = time.time()
import os
import random
import re
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop, ChatMemberHandler
from telegram.request import HTTPXRequest
from telegram.constants import ChatType
from typing import Dict, List, Tuple
import json
import threading

# --- Logging Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
JSON_DB_PATH = os.getenv("JSON_DB_PATH", "database.json")
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID")) 
except (TypeError, ValueError):
    ADMIN_USER_ID = 0 
    logger.warning("⚠️ ADMIN_USER_ID not set or invalid. Admin features will be disabled.")

# --- Configuration (Capped at 8 letters and using only 8-letter for hard/extreme) ---
DIFFICULTY_CONFIG = {
    'easy': {'length': 4, 'max_guesses': 30, 'base_points': 5, 'example': 'GAME'},
    'medium': {'length': 5, 'max_guesses': 30, 'base_points': 10, 'example': 'APPLE'},
    'hard': {'length': 8, 'max_guesses': 30, 'base_points': 20, 'example': 'FOOTBALL'},
    'extreme': {'length': 8, 'max_guesses': 30, 'base_points': 50, 'example': 'FOOTBALL'} 
}

# --- Log Channel Configuration ---
LOG_CHANNEL_ID_ENV = os.getenv("LOG_CHANNEL_ID", "-1003978131834")
try:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_ENV)
except (TypeError, ValueError):
    LOG_CHANNEL_ID = -1003978131834

async def send_log_notification(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    """Sends log notifications to the designated Telegram log channel."""
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send log notification to channel {LOG_CHANNEL_ID}: {e}")

# --- Load Word List from word.txt ---
WORD_FILE_PATH = os.path.join(os.path.dirname(__file__), "word.txt")
VALID_WORDS_SET = set()
WORDS_BY_LENGTH: Dict[int, List[str]] = {}

if os.path.exists(WORD_FILE_PATH):
    with open(WORD_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            cleaned_word = "".join(filter(str.isalpha, line.strip().upper()))
            if cleaned_word:
                VALID_WORDS_SET.add(cleaned_word)
                length = len(cleaned_word)
                if length in [c['length'] for c in DIFFICULTY_CONFIG.values()]:
                    WORDS_BY_LENGTH.setdefault(length, []).append(cleaned_word)
    logger.info(f"✅ Loaded {len(VALID_WORDS_SET)} total words from word.txt")
else:
    logger.error(f"❌ word.txt not found at {WORD_FILE_PATH}")
         
# --- JSON Database Manager Class ---
class JSONDatabaseManager:
    """Handles all interactions with JSON file based database with time-based leaderboards."""
    def __init__(self, db_path: str = "database.json"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.data = {
            "leaderboard": {},
            "active_games": {},
            "known_chats": {},
            "started_users": [],
            "banned_users": [],
            "bot_admins": [],
            "seek_oauth": {}
        }
        self._load()

    def _load(self):
        with self.lock:
            if os.path.exists(self.db_path):
                try:
                    with open(self.db_path, "r", encoding="utf-8") as f:
                        loaded_data = json.load(f)
                        if isinstance(loaded_data, dict):
                            self.data["leaderboard"] = loaded_data.get("leaderboard", {})
                            self.data["active_games"] = loaded_data.get("active_games", {})
                            self.data["known_chats"] = loaded_data.get("known_chats", {})
                            self.data["started_users"] = loaded_data.get("started_users", [])
                            self.data["banned_users"] = loaded_data.get("banned_users", [])
                            self.data["bot_admins"] = loaded_data.get("bot_admins", [])
                            self.data["seek_oauth"] = loaded_data.get("seek_oauth", {})
                    logger.info(f"✅ JSON Database loaded successfully from {self.db_path}.")
                except Exception as e:
                    logger.error(f"❌ Failed to load JSON Database from {self.db_path}: {e}")
            else:
                logger.info(f"ℹ️ JSON Database file {self.db_path} does not exist yet. Initializing new structure.")
                self._save_unlocked()

    def _save_unlocked(self):
        temp_path = f"{self.db_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(temp_path, self.db_path)
        except Exception as e:
            logger.error(f"❌ Failed to save JSON Database to {self.db_path}: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def _save(self):
        with self.lock:
            self._save_unlocked()

    def _check_and_reset_period(self, user_data: dict, period: str, now: datetime):
        if period == "global":
            return

        last_win_str = user_data.get(f"last_win_date_{period}")
        if not last_win_str:
            return

        try:
            last_win_date = datetime.fromisoformat(last_win_str)
            if last_win_date.tzinfo is None:
                last_win_date = last_win_date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last_win_date = None

        if period == "daily":
            threshold = now - timedelta(days=1)
        elif period == "weekly":
            threshold = now - timedelta(weeks=1)
        elif period == "monthly":
            threshold = now - timedelta(days=30)
        else:
            return

        if last_win_date and last_win_date < threshold:
            user_data[f"points_{period}"] = 0
            user_data[f"wins_{period}"] = 0

    def update_leaderboard(self, user_id: int, username: str, points_to_add: int):
        with self.lock:
            now = datetime.now(timezone.utc)
            user_key = str(user_id)
            if user_key not in self.data["leaderboard"]:
                self.data["leaderboard"][user_key] = {
                    "user_id": user_id,
                    "username": username or "Unknown",
                    "points_global": 0,
                    "wins_global": 0,
                    "points_daily": 0,
                    "wins_daily": 0,
                    "points_weekly": 0,
                    "wins_weekly": 0,
                    "points_monthly": 0,
                    "wins_monthly": 0,
                }

            user_data = self.data["leaderboard"][user_key]
            if username:
                user_data["username"] = username
            user_data["points_global"] = user_data.get("points_global", 0) + points_to_add
            user_data["wins_global"] = user_data.get("wins_global", 0) + 1

            for period in ["daily", "weekly", "monthly"]:
                self._check_and_reset_period(user_data, period, now)
                user_data[f"points_{period}"] = user_data.get(f"points_{period}", 0) + points_to_add
                user_data[f"wins_{period}"] = user_data.get(f"wins_{period}", 0) + 1
                user_data[f"last_win_date_{period}"] = now.isoformat()

            self._save_unlocked()

    def get_leaderboard_data(self, period: str, limit=10) -> List[Tuple[str, int, int]]:
        with self.lock:
            now = datetime.now(timezone.utc)
            points_key = f"points_{period}"
            wins_key = f"wins_{period}"

            result = []
            for user_data in self.data["leaderboard"].values():
                if period != "global":
                    self._check_and_reset_period(user_data, period, now)

                points = user_data.get(points_key, 0)
                wins = user_data.get(wins_key, 0)

                if points > 0 or period == "global":
                    result.append((user_data.get("username", "Unknown"), points, wins))

            result.sort(key=lambda x: x[1], reverse=True)
            return result[:limit]

    def get_game_state(self, chat_id: int) -> Dict | None:
        with self.lock:
            game = self.data["active_games"].get(str(chat_id))
            return game.copy() if game else None

    def save_game_state(self, chat_id: int, state: Dict):
        with self.lock:
            state_to_save = {"chat_id": chat_id, **state}
            self.data["active_games"][str(chat_id)] = state_to_save
            self._save_unlocked()

    def delete_game_state(self, chat_id: int):
        with self.lock:
            if str(chat_id) in self.data["active_games"]:
                del self.data["active_games"][str(chat_id)]
                self._save_unlocked()

    def add_chat(self, chat_id: int, chat_type: str, title: str = "", invite_link: str = "", date: float = 0):
        with self.lock:
            self.data["known_chats"][str(chat_id)] = {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "title": title or "",
                "invite_link": invite_link or "",
                "last_active": date
            }
            self._save_unlocked()

    def remove_chat(self, chat_id: int):
        with self.lock:
            if str(chat_id) in self.data["known_chats"]:
                del self.data["known_chats"][str(chat_id)]
                self._save_unlocked()

    def get_all_chat_ids(self) -> List[int]:
        with self.lock:
            return [chat["chat_id"] for chat in self.data["known_chats"].values()]

    def get_chats_by_type(self, chat_types: list) -> List[dict]:
        with self.lock:
            return [
                chat for chat in self.data["known_chats"].values()
                if chat.get("chat_type") in chat_types
            ]

    def has_started(self, user_id: int) -> bool:
        with self.lock:
            return user_id in self.data.get("started_users", [])

    def mark_started(self, user_id: int):
        with self.lock:
            if "started_users" not in self.data:
                self.data["started_users"] = []
            if user_id not in self.data["started_users"]:
                self.data["started_users"].append(user_id)
                self._save_unlocked()

    def is_banned(self, user_id: int) -> bool:
        with self.lock:
            return user_id in self.data.get("banned_users", [])

    def ban_user(self, user_id: int):
        with self.lock:
            if "banned_users" not in self.data:
                self.data["banned_users"] = []
            if user_id not in self.data["banned_users"]:
                self.data["banned_users"].append(user_id)
                self._save_unlocked()

    def unban_user(self, user_id: int):
        with self.lock:
            if "banned_users" in self.data and user_id in self.data["banned_users"]:
                self.data["banned_users"].remove(user_id)
                self._save_unlocked()

    def is_bot_admin(self, user_id: int) -> bool:
        with self.lock:
            return user_id in self.data.get("bot_admins", [])

    def add_bot_admin(self, user_id: int):
        with self.lock:
            if "bot_admins" not in self.data:
                self.data["bot_admins"] = []
            if user_id not in self.data["bot_admins"]:
                self.data["bot_admins"].append(user_id)
                self._save_unlocked()

    def remove_bot_admin(self, user_id: int):
        with self.lock:
            if "bot_admins" in self.data and user_id in self.data["bot_admins"]:
                self.data["bot_admins"].remove(user_id)
                self._save_unlocked()

    def has_seek_oauth(self, chat_id: int, user_id: int) -> bool:
        with self.lock:
            chat_key = str(chat_id)
            users = self.data.get("seek_oauth", {}).get(chat_key, [])
            return user_id in users

    def grant_seek_oauth(self, chat_id: int, user_id: int):
        with self.lock:
            if "seek_oauth" not in self.data:
                self.data["seek_oauth"] = {}
            chat_key = str(chat_id)
            if chat_key not in self.data["seek_oauth"]:
                self.data["seek_oauth"][chat_key] = []
            if user_id not in self.data["seek_oauth"][chat_key]:
                self.data["seek_oauth"][chat_key].append(user_id)
                self._save_unlocked()

    def revoke_seek_oauth(self, chat_id: int, user_id: int):
        with self.lock:
            chat_key = str(chat_id)
            if "seek_oauth" in self.data and chat_key in self.data["seek_oauth"]:
                if user_id in self.data["seek_oauth"][chat_key]:
                    self.data["seek_oauth"][chat_key].remove(user_id)
                    self._save_unlocked()

    def add_score(self, user_id: int, username: str, points: int):
        with self.lock:
            user_key = str(user_id)
            if user_key not in self.data["leaderboard"]:
                self.data["leaderboard"][user_key] = {
                    "user_id": user_id,
                    "username": username or "Unknown",
                    "points_global": 0,
                    "wins_global": 0,
                    "points_daily": 0,
                    "wins_daily": 0,
                    "points_weekly": 0,
                    "wins_weekly": 0,
                    "points_monthly": 0,
                    "wins_monthly": 0,
                }
            user_data = self.data["leaderboard"][user_key]
            if username:
                user_data["username"] = username
            user_data["points_global"] = max(0, user_data.get("points_global", 0) + points)
            for period in ["daily", "weekly", "monthly"]:
                user_data[f"points_{period}"] = max(0, user_data.get(f"points_{period}", 0) + points)
            self._save_unlocked()

    def remove_score(self, user_id: int, points: int):
        with self.lock:
            user_key = str(user_id)
            if user_key in self.data["leaderboard"]:
                user_data = self.data["leaderboard"][user_key]
                user_data["points_global"] = max(0, user_data.get("points_global", 0) - points)
                for period in ["daily", "weekly", "monthly"]:
                    user_data[f"points_{period}"] = max(0, user_data.get(f"points_{period}", 0) - points)
                self._save_unlocked()

    def get_user_score(self, user_id: int) -> dict | None:
        with self.lock:
            user_key = str(user_id)
            return self.data["leaderboard"].get(user_key)

    def get_user_by_username(self, username: str) -> dict | None:
        with self.lock:
            clean_name = username.lstrip('@').lower()
            for user_data in self.data["leaderboard"].values():
                u_name = user_data.get("username", "")
                if u_name and u_name.lower() == clean_name:
                    return user_data
            return None

    def get_stats(self) -> dict:
        with self.lock:
            all_users = set(self.data.get("started_users", []))
            for u_id in self.data["leaderboard"].keys():
                try:
                    all_users.add(int(u_id))
                except ValueError:
                    pass
            for chat in self.data["known_chats"].values():
                if chat.get("chat_type") in ["PRIVATE", "private"]:
                    all_users.add(chat.get("chat_id"))

            group_count = sum(
                1 for chat in self.data["known_chats"].values()
                if chat.get("chat_type") in ["GROUP", "SUPERGROUP", "group", "supergroup"]
            )
            return {
                "total_users": len(all_users),
                "total_groups": group_count
            }

# --- Initialize JSON Database Manager ---
db_manager = None
try:
    db_manager = JSONDatabaseManager(JSON_DB_PATH)
    logger.info(f"✅ JSON Database initialized successfully at {JSON_DB_PATH}.")
except Exception as e:
    logger.error(f"❌ FATAL: Could not initialize JSON Database. Error: {e}")
    db_manager = None

mongo_manager = db_manager

# --- Core Game Logic Functions ---
# (get_feedback and calculate_points remain unchanged)

def get_feedback(secret_word: str, guess: str) -> str:
    """Generates the Wordle-style color-coded feedback (🟩, 🟨, 🟥)."""
    length = len(secret_word)
    feedback = ['🟥'] * length 
    remaining_letters = {}
    
    for letter in secret_word:
        remaining_letters[letter] = remaining_letters.get(letter, 0) + 1

    # First pass: Green (Correct position)
    for i in range(length):
        if i < len(guess) and guess[i] == secret_word[i]:
            feedback[i] = '🟩'
            remaining_letters[guess[i]] -= 1

    # Second pass: Yellow (Correct letter, wrong position)
    for i in range(length):
        if feedback[i] == '🟥' and i < len(guess):
            letter = guess[i]
            if letter in remaining_letters and remaining_letters[letter] > 0:
                feedback[i] = '🟨'
                remaining_letters[letter] -= 1
    
    return "".join(feedback)

def calculate_points(difficulty: str, guesses: int) -> int:
    """Calculates points based on difficulty and efficiency."""
    config = DIFFICULTY_CONFIG[difficulty]
    base = config['base_points']
    # Higher bonus for fewer guesses
    bonus = max(0, 10 - (guesses - 1) * 2) 
    return base + bonus

async def start_new_game_logic(chat_id: int, difficulty: str) -> Tuple[bool, str]:
    if not db_manager: return False, "❌ *Database Error*. Game cannot be started without database access."
    
    difficulty = difficulty.lower()
    if difficulty not in DIFFICULTY_CONFIG:
        difficulty = 'medium'
        
    config = DIFFICULTY_CONFIG[difficulty]
    length = config['length']
    word_list = WORDS_BY_LENGTH.get(length)
    
    if not word_list:
        return False, f"❌ *Error*: No secret words found for **{difficulty}** ({length} letters). Contact admin."
    
    # Select a secret word from the list
    secret_word = random.choice(word_list)
    
    initial_state = {
        'word': secret_word,
        'difficulty': difficulty,
        'guesses_made': 0,
        'max_guesses': config['max_guesses'],
        'guess_history': [],
        'guessed_words': [] # NEW: To track unique words guessed
    }
    db_manager.save_game_state(chat_id, initial_state)
    
    return True, (
        f"**✨ New Word Rush Challenge!**\n"
        f"-------------------------------------\n"
        f"🎯 Difficulty: **{difficulty.capitalize()}**\n"
        f"📜 Word Length: **{length} letters** (Example: `{config['example']}`)\n"
        f"➡️ *Send your {length}-letter guess directly to the chat!*"
    )

async def process_guess_logic(chat_id: int, guess: str) -> Tuple[str, bool, str, int, List[str]]:
    """Processes a user's guess and returns feedback, win status, and points."""
    if not db_manager: return "", False, "Database Error.", 0, []

    game = db_manager.get_game_state(chat_id)
    if not game:
        return "", False, "No active game.", 0, []
    
    secret_word = game['word']
    # Guess is already cleaned/uppercase by the MessageHandler filters
    guess_clean = guess.upper()

    config = DIFFICULTY_CONFIG[game['difficulty']]
    length = config['length']
    
    # 1. Validation for length (Should match game length)
    if len(guess_clean) != length:
        # User message for incorrect length
        return "", False, f"❌ **`{guess.upper()}`** *must be exactly* **{length}** *letters long*.", 0, game.get('guess_history', [])
    
    # 2. Check if word is valid (exists in word.txt)
    if VALID_WORDS_SET and guess_clean not in VALID_WORDS_SET:
        return "", False, f"❌ **`{guess.upper()}`** is a **wrong word**! It is not in word.txt.", 0, game.get('guess_history', [])

    # 3. Check if word has already been guessed
    if guess_clean in game.get('guessed_words', []):
        # User message for duplicate guess
        return "", False, f"❌ **`{guess.upper()}`** *already guessed! Try a new word*.", 0, game.get('guess_history', [])

    
    game['guesses_made'] += 1
    game['guessed_words'].append(guess_clean) # Add the valid guess to the list
    
    # 3. Generate Feedback and update history
    feedback_str = get_feedback(secret_word, guess_clean)
    # Storing in the required format: Blocks - WORD
    game['guess_history'].append(f" `{feedback_str}` - **{guess_clean}**") 
    
    # 4. Check for Win
    if guess_clean == secret_word:
        guesses = game['guesses_made']
        points = calculate_points(game['difficulty'], guesses)
        game_word_for_loss = game['word']
        db_manager.delete_game_state(chat_id)
        return feedback_str, True, "WIN", points, game['guess_history']

    # 5. Check for Loss
    remaining = game['max_guesses'] - game['guesses_made']
    
    if remaining <= 0:
        game_word_for_loss = game['word']
        db_manager.delete_game_state(chat_id)
        # For loss, we return the secret word as status
        return feedback_str, False, f"LOSS_WORD:{game_word_for_loss}", 0, game['guess_history']
    
    # Status for ongoing game
    db_manager.save_game_state(chat_id, game)
    return feedback_str, False, f"Guesses left: **{remaining}**", 0, game['guess_history']

# --- Telegram UI & Handler Functions (All Unchanged) ---

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Any user is considered "admin" in a private chat for commands like /end and /difficulty
    if update.effective_chat.type == ChatType.PRIVATE:
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

# --- Keyboard Functions (Unchanged) ---

def get_start_keyboard(bot_username: str = "narzowordseekbot"):
    keyboard = [
        [InlineKeyboardButton("❓ Help & Info", callback_data="show_help_menu")],
        [
            InlineKeyboardButton("💬 Report Bugs", url="https://t.me/Itz_Alaric"),
            InlineKeyboardButton("📢 Updates Channel", url="http://t.me/the_Jarvis_bots")
        ],
        [InlineKeyboardButton("➕ Add Bot to Group", url=f"https://t.me/{bot_username}?startgroup=true")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_help_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📝 How to Play", callback_data="show_how_to_play")],
        [InlineKeyboardButton("📘 Commands List", callback_data="show_commands")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard_menu")],
        [InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_play_again_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎯 Start New Game", callback_data="new_game_menu")] 
    ]
    return InlineKeyboardMarkup(keyboard)

def get_new_game_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("⭐ Easy (4 letters)", callback_data="start_easy"),
            InlineKeyboardButton("🌟 Medium (5 letters)", callback_data="start_medium")
        ],
        [
            InlineKeyboardButton("🔥 Hard (8 letters)", callback_data="start_hard"),
            InlineKeyboardButton("💎 Extreme (8 letters, High Pts)", callback_data="start_extreme")
        ],
        [InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_leaderboard_menu_keyboard(): 
    keyboard = [
        [
            InlineKeyboardButton("☀️ Daily", callback_data="show_leaderboard_daily"),
            InlineKeyboardButton("📅 Weekly", callback_data="show_leaderboard_weekly"),
        ],
        [
            InlineKeyboardButton("🗓️ Monthly", callback_data="show_leaderboard_monthly"),
            InlineKeyboardButton("🌎 Global", callback_data="show_leaderboard_global"),
        ],
        [InlineKeyboardButton("🔙 Back to Help", callback_data="show_help_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


# --- Helper for Safe Callback Message Editing ---

async def safe_edit_message(
    target,
    text: str,
    reply_markup=None,
    parse_mode: str = 'Markdown',
    disable_web_page_preview=None
) -> None:
    """
    Safely edits a message in response to a callback query or update.
    Handles photo/media messages by editing caption, suppresses 'Message is not modified'
    errors, and falls back to reply_text if editing fails.
    """
    query = getattr(target, 'callback_query', None) if hasattr(target, 'callback_query') and target.callback_query else target
    message = getattr(query, 'message', None) if hasattr(query, 'message') else None

    is_media = False
    if message:
        if getattr(message, 'photo', None) or getattr(message, 'caption', None) is not None:
            is_media = True

    try:
        if is_media and hasattr(query, 'edit_message_caption'):
            kwargs = {}
            if parse_mode:
                kwargs['parse_mode'] = parse_mode
            if reply_markup is not None:
                kwargs['reply_markup'] = reply_markup
            await query.edit_message_caption(caption=text, **kwargs)
            return
        elif hasattr(query, 'edit_message_text'):
            kwargs = {}
            if parse_mode:
                kwargs['parse_mode'] = parse_mode
            if reply_markup is not None:
                kwargs['reply_markup'] = reply_markup
            if disable_web_page_preview is not None:
                kwargs['disable_web_page_preview'] = disable_web_page_preview
            await query.edit_message_text(text=text, **kwargs)
            return
    except error.BadRequest as e:
        err_str = str(e).lower()
        if 'message is not modified' in err_str:
            return
        if 'there is no text in the message to edit' in err_str or 'message to edit not found' in err_str:
            if hasattr(query, 'edit_message_caption'):
                try:
                    kwargs = {}
                    if parse_mode:
                        kwargs['parse_mode'] = parse_mode
                    if reply_markup is not None:
                        kwargs['reply_markup'] = reply_markup
                    await query.edit_message_caption(caption=text, **kwargs)
                    return
                except error.BadRequest as e2:
                    if 'message is not modified' in str(e2).lower():
                        return
                    logger.warning(f'Failed to edit message caption in fallback: {e2}')
        logger.warning(f'safe_edit_message edit failed ({e}), attempting fallback message...')

    # Fallback sending new message
    try:
        if message and hasattr(message, 'reply_text'):
            kwargs = {}
            if parse_mode:
                kwargs['parse_mode'] = parse_mode
            if reply_markup is not None:
                kwargs['reply_markup'] = reply_markup
            if disable_web_page_preview is not None:
                kwargs['disable_web_page_preview'] = disable_web_page_preview
            await message.reply_text(text=text, **kwargs)
        elif hasattr(target, 'effective_message') and target.effective_message:
            kwargs = {}
            if parse_mode:
                kwargs['parse_mode'] = parse_mode
            if reply_markup is not None:
                kwargs['reply_markup'] = reply_markup
            if disable_web_page_preview is not None:
                kwargs['disable_web_page_preview'] = disable_web_page_preview
            await target.effective_message.reply_text(text=text, **kwargs)
    except Exception as fallback_err:
        logger.error(f'Failed to fallback reply_text in safe_edit_message: {fallback_err}')

# --- Leaderboard Utility Function (Unchanged) ---

async def display_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    """Fetches and displays the leaderboard for the given period."""
    if not db_manager:
        await safe_edit_message(update, "❌ *Database Error*. Cannot fetch leaderboard.")
        return

    data = db_manager.get_leaderboard_data(period=period, limit=10)
    
    title = period.capitalize() if period != 'global' else 'Global'
    
    if not data:
        message = f"🏆 **{title} Leaderboard**\n\n*No scores recorded for this period yet.*"
    else:
        message = f"🏆 **{title} Leaderboard** (Top 10)\n"
        message += "-------------------------------------\n"
        for i, (username, points, wins) in enumerate(data):
            rank_style = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"**{i+1}.**"
            name = f"@{username}" if username else f"User ID `{data[i][0]}`"
            message += f"{rank_style} {name} - **`{points}`** pts ({wins} wins)\n"
            
    # Send as a new message if it's a command, or edit if it's a callback
    if update.callback_query:
        await safe_edit_message(update, message, reply_markup=get_leaderboard_menu_keyboard(), parse_mode="Markdown")
    else:
        await update.message.reply_text(message, reply_markup=get_leaderboard_menu_keyboard(), parse_mode='Markdown')

# --- Command Handlers (All Unchanged) ---

IMAGE_FILE_PATH = os.path.join(os.path.dirname(__file__), "IMG_20261012_190913_522.jpg")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not update.effective_message:
        return

    chat_title = chat.title or "Private"
    chat_type = chat.type.name if hasattr(chat.type, "name") else str(chat.type)

    if db_manager:
        db_manager.add_chat(chat.id, chat_type, chat_title, "", update.effective_message.date.timestamp())

    if user and db_manager:
        if not db_manager.has_started(user.id):
            db_manager.mark_started(user.id)
            user_info = f"@{user.username}" if user.username else f"{user.first_name} (ID: `{user.id}`)"
            log_msg = f"🚀 **Bot Started**\n👤 **User:** {user_info}\n💬 **Chat:** {chat_title} (`{chat.id}`)"
            await send_log_notification(context, log_msg)

    start_caption = (
        "Welcome to WordSeek!\n\n"
        "A fun and competitive Wordle-style game that you can play directly on Telegram.\n\n"
        "> Quick Start:\n"
        "> • Use /new to start a new game\n"
        "> • Add me to a group with admin permissions to play with friends\n"
        "> • Use /help for detailed instructions and command list\n\n"
        "Ready to test your word skills? Let's play!"
    )

    bot_username = context.bot.username or "narzowordseekbot"
    reply_markup = get_start_keyboard(bot_username)

    if os.path.exists(IMAGE_FILE_PATH):
        try:
            with open(IMAGE_FILE_PATH, "rb") as photo_file:
                await update.effective_message.reply_photo(
                    photo=photo_file,
                    caption=start_caption,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            return
        except Exception as e:
            logger.error(f"Failed to send start photo: {e}")

    await update.effective_message.reply_text(
        start_caption,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def new_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if db_manager and update.effective_message:
        db_manager.add_chat(chat_id, update.effective_chat.type.name, update.effective_message.date.timestamp())

    difficulty = context.args[0].lower() if context.args else 'medium'
    
    if db_manager and db_manager.get_game_state(chat_id):
        await update.message.reply_text("⏳ *A game is already active*. Use **/end** to stop it first.")
        return

    success, message = await start_new_game_logic(chat_id, difficulty)
    if success:
        user = update.effective_user
        user_info = f"@{user.username}" if user and user.username else (user.first_name if user else "User")
        log_msg = f"🎮 **New Game Started**\n🎯 **Difficulty:** {difficulty.capitalize()}\n👤 **Started By:** {user_info}\n💬 **Chat ID:** `{chat_id}`"
        await send_log_notification(context, log_msg)
    await update.message.reply_text(message, parse_mode='Markdown')

async def end_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if not db_manager or not db_manager.get_game_state(chat_id):
        await update.effective_message.reply_text("❌ *No game is currently running to end*.", parse_mode='Markdown')
        return

    is_admin = await is_group_admin(update, context)
    has_oauth = db_manager.has_seek_oauth(chat_id, user.id) if db_manager else False
    is_owner_or_botadmin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))

    if not (is_admin or has_oauth or is_owner_or_botadmin):
        await update.effective_message.reply_text("🚨 You don't have permission to end the game. Group admins or authorized members (/seekoauth) can end it.", parse_mode='Markdown')
        return

    game_state = db_manager.get_game_state(chat_id)
    word = game_state.get('word', 'UNKNOWN')
    db_manager.delete_game_state(chat_id)
    
    user_info = f"@{user.username}" if user and user.username else (user.first_name if user else "User")
    log_msg = f"🛑 **Game Ended Manually**\n👤 **By:** {user_info}\n💬 **Chat ID:** `{chat_id}`\n🔑 **Word Was:** `{word}`"
    await send_log_notification(context, log_msg)

    await update.effective_message.reply_text(
        f"🛑 **Game Ended!**\n*The secret word was:* **`{word}`**.",
        parse_mode='Markdown'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the current game status and guess history."""
    chat_id = update.effective_chat.id
    if not db_manager:
        await update.message.reply_text("❌ *Database Error*. Cannot fetch game status.")
        return

    game_state = db_manager.get_game_state(chat_id)
    if not game_state:
        await update.message.reply_text("🎯 *No active game*. Use **/new** to start a challenge!")
        return
    
    guess_history = game_state.get('guess_history', [])
    
    if not guess_history:
        history_display = "*No guesses made yet!*"
    else:
        history_display = "\n".join(guess_history)

    remaining = game_state['max_guesses'] - game_state['guesses_made']
    
    reply_text = (
        f"**📊 Current Word Rush Status**\n"
        f"-------------------------------------\n"
        f"Difficulty: **{game_state['difficulty'].capitalize()}**\n"
        f"Word Length: **{len(game_state['word'])} letters**\n"
        f"Guesses: **`{game_state['guesses_made']}`** / **`{game_state['max_guesses']}`**\n"
        f"Remaining: **`{remaining}`**\n\n"
        f"📜 **Guess History:**\n"
        f"{history_display}"
    )
    
    await update.message.reply_text(reply_text, parse_mode='Markdown')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the leaderboard menu or the global leaderboard directly."""
    if db_manager and update.effective_message:
        db_manager.add_chat(update.effective_chat.id, update.effective_chat.type.name, update.effective_message.date.timestamp())

    # If arguments are provided (e.g., /leaderboard daily), show that specific one
    if context.args and context.args[0].lower() in ['daily', 'weekly', 'monthly', 'global']:
        period = context.args[0].lower()
        await display_leaderboard(update, context, period)
        return

    # Otherwise, show the leaderboard menu
    message = "🏆 **Global Leaderboard**\n\n*Choose a period below to view the rankings!*"
    await update.message.reply_text(message, reply_markup=get_leaderboard_menu_keyboard(), parse_mode='Markdown')

async def difficulty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows available difficulty levels and their settings."""
    chat_id = update.effective_chat.id

    if not await is_group_admin(update, context):
        await update.message.reply_text("🚨 *Admin Check Failed*. You must be an **Admin** to view or change settings.", parse_mode='Markdown')
        return

    message = "**⚙️ Word Rush Difficulty Settings**\n"
    message += "-------------------------------------\n"
    
    for level, config in DIFFICULTY_CONFIG.items():
        message += f"**{level.capitalize()}**:\n"
        message += f"   - Word Length: **{config['length']}** letters\n"
        message += f"   - Max Guesses: **{config['max_guesses']}**\n"
        message += f"   - Base Points: **{config['base_points']}**\n"
        message += f"   - Example: `{config['example']}`\n\n"

    message += "👉 *Use* `/new <level>` *to start a game with a specific difficulty.* (e.g., `/new hard`)"

    await update.message.reply_text(message, parse_mode='Markdown')


# --- Broadcast Command (Unchanged, Admin only) ---

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message to all known chats (Admin only)."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/broadcast <your message here>`", parse_mode='Markdown')
        return
    
    if not db_manager:
        await update.message.reply_text("❌ Database error. Cannot retrieve chat list.")
        return

    message_to_send = " ".join(context.args)
    chat_ids = db_manager.get_all_chat_ids()
    
    success_count = 0
    fail_count = 0
    
    await update.message.reply_text(f"📢 *Attempting to broadcast message to* **{len(chat_ids)}** *chats...*")

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_to_send, parse_mode='Markdown')
            success_count += 1
        except error.Forbidden:
            logger.warning(f"Failed to send broadcast to chat {chat_id}: Bot blocked.")
            fail_count += 1
        except Exception as e:
            logger.error(f"Failed to send broadcast to chat {chat_id}: {e}")
            fail_count += 1
            
    await update.message.reply_text(f"✅ **Broadcast Complete**\nSuccessful: **{success_count}**\nFailed: **{fail_count}**", parse_mode='Markdown')

# --- Callback Handler (Unchanged) ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer() 
    chat_id = query.message.chat_id
    
    if query.data == "back_to_start":
        await safe_edit_message(query,
            "👋 *Hello! I'm* **WordRush Bot** 🤖\n"
            "-------------------------------------\n"
            "The **Ultimate Word Challenge** on Telegram!\n\n"
            "📜 **Goal:** *Guess the secret word using hints (🟩/🟨/🟥).*\n"
            "🏆 **Compete:** *Win to earn points and climb the Global Leaderboard!* 🌐\n\n"
            "👉 Tap **/new** or the button below to start your rush!\n"
            "-------------------------------------",
            reply_markup=get_start_keyboard(),
            parse_mode='Markdown'
        )
    
    elif query.data == "show_help_menu":
        await safe_edit_message(query,
            "📖 **WordRush Help Center**\n"
            "-------------------------------------\n"
            "*Choose a topic below to get assistance.*\n"
            "*For any issue, please ask in the Report group!*",
            reply_markup=get_help_menu_keyboard(),
            parse_mode='Markdown'
        )

    elif query.data == "show_how_to_play":
        commands_list = (
            "🤔 **How to Play Word Rush** ❓\n"
            "-------------------------------------\n"
            "1. **The Word:** *Guess a secret word*, length depends on difficulty (4, 5, or 8 letters).\n\n"
            "2. **The Hints (`Boxes - Word`):**\n"
            "   • 🟢 *Green* = Correct letter, **Right Place**.\n"
            "   • 🟡 *Yellow* = Correct letter, **Wrong Place**.\n"
            "   • 🔴 *Red* = Letter **Not in the Word**.\n\n"
            "3. **The Game:** You have *30 guesses*. The person who wins with the fewest guesses gets the most points! 🥇"
        )
        await safe_edit_message(query, commands_list, reply_markup=get_help_menu_keyboard(), parse_mode='Markdown')

    elif query.data == "show_commands":
        commands_list = (
            "📘 **Word Rush Commands List**\n"
            "-------------------------------------\n"
            "• **/new** [difficulty] → *Start a game*.\n"
            "• **/status** → *Show current game status and history* (New Feature!).\n"
            "• **/leaderboard** [period] → *Show global/daily/weekly/monthly rankings*.\n"
            "• **/end** → *End current game* (Admin Only / DM).\n"
            "• **/difficulty** → *Show difficulty settings* (Admin Only / DM).\n"
        )
        await safe_edit_message(query, commands_list, reply_markup=get_help_menu_keyboard(), parse_mode='Markdown')
        
    elif query.data == "show_leaderboard_menu":
        await safe_edit_message(query,
            "🏆 **Leaderboard Selection**\n"
            "-------------------------------------\n"
            "*Select the ranking period you wish to view.*",
            reply_markup=get_leaderboard_menu_keyboard(),
            parse_mode='Markdown'
        )
        
    elif query.data.startswith("show_leaderboard_"):
        period = query.data.split('_')[-1]
        await display_leaderboard(update, context, period)

    elif query.data == "new_game_menu":
        await safe_edit_message(query,
            "🎯 **Select Your Challenge Level:**\n"
            "*Choose the word length and point value.*",
            reply_markup=get_new_game_keyboard(),
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("start_"):
        difficulty = query.data.split('_')[1]
        
        if db_manager and db_manager.get_game_state(chat_id):
            await safe_edit_message(query, "⏳ *A game is already active*. Use **/end** to stop it first.")
            return

        success, message = await start_new_game_logic(chat_id, difficulty)
        if success:
            user = update.effective_user
            user_info = f"@{user.username}" if user and user.username else (user.first_name if user else "User")
            log_msg = f"🎮 **New Game Started (Button)**\n🎯 **Difficulty:** {difficulty.capitalize()}\n👤 **Started By:** {user_info}\n💬 **Chat ID:** `{chat_id}`"
            await send_log_notification(context, log_msg)
            await safe_edit_message(query, message, parse_mode='Markdown')
        else:
            await safe_edit_message(query, f"❌ *Game start failed*: {message}", parse_mode='Markdown')

# --- Updated Guess Handler (Handles the new error message) ---
    elif query.data.startswith("groups_page_"):
        page = int(query.data.split("_")[-1])
        await render_groups_page(update, context, page=page)

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    guess = update.message.text.strip()
    
    username = user.username or user.first_name
    logger.info(f"Guess received in chat {chat_id} from {username}: {guess}")

    if not db_manager:
        return

    game_state = db_manager.get_game_state(chat_id)
    if not game_state:
        return 

    # Process guess
    feedback, is_win, status_message, points, guess_history = await process_guess_logic(chat_id, guess)
    
    # 1. Handle validation errors (Incorrect length OR Duplicate guess)
    if status_message.startswith("❌"):
        await update.message.reply_text(status_message, parse_mode='Markdown')
        return

    reply_markup = None
    
    # 2. Construct the full history display
    game_history_display = "\n".join(guess_history)
    
    # 3. Handle Win/Loss/Ongoing
    
    if is_win:
        word_was = guess_history[-1].split(' - ')[-1].replace('**', '').strip() 
        
        # This function now updates global, daily, weekly, and monthly scores
        db_manager.update_leaderboard(user.id, username, points)
        log_msg = f"🏆 **Game Won!**\n👤 **Winner:** @{username} (`{user.id}`)\n💬 **Chat ID:** `{chat_id}`\n🔑 **Word:** `{word_was}`\n🎯 **Points:** `{points}` ({len(guess_history)} attempts)"
        await send_log_notification(context, log_msg)
        
        reply_text = (
            f"**🏆 GAME WON! 🥳**\n"
            f"-------------------------------------\n"
            f"*Congratulations* **{username}**!\n"
            f"You cracked the code in **{len(guess_history)}** attempts!\n"
            f"✨ Points earned: **`{points}`**\n\n"
            f"📜 **Final Board:**\n"
            f"{game_history_display}\n\n" 
            f"✅ *The secret word was:* **`{word_was}`**"
        )
        reply_markup = get_play_again_keyboard()

    elif status_message.startswith("LOSS_WORD:"):
        word_was = status_message.split(":")[1]
        log_msg = f"💔 **Game Over (Loss)**\n👤 **Last Player:** @{username} (`{user.id}`)\n💬 **Chat ID:** `{chat_id}`\n🔑 **Word Was:** `{word_was}`"
        await send_log_notification(context, log_msg)
        
        reply_text = (
            f"💔 **GAME OVER! 😭**\n"
            f"-------------------------------------\n"
            f"*Maximum guesses reached* (**{game_state['max_guesses']}**).\n\n"
            f"📜 **Final Board:**\n"
            f"{game_history_display}\n\n" 
            f"❌ *The secret word was:* **`{word_was}`**"
        )
        reply_markup = get_play_again_keyboard()

    else:
        # Ongoing game message (Show full history + status)
        
        reply_text = (
            f"**Word Rush Challenge** 🎯\n"
            f"-------------------------------------\n"
            f"Attempts: **`{len(guess_history)}`** / **`{game_state['max_guesses']}`**\n\n"
            f"📜 **Guess History:**\n"
            f"{game_history_display}\n\n" 
            f"👉 {status_message}" # Displays: Guesses left: **27**
        )
    
    await update.message.reply_text(
        reply_text, 
        reply_markup=reply_markup, 
        parse_mode='Markdown'
    )

# --- Main Bot Runner ---

# --- Network / Timeout Settings ---
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "30.0"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30.0"))
HTTP_WRITE_TIMEOUT = float(os.getenv("HTTP_WRITE_TIMEOUT", "30.0"))
HTTP_POOL_TIMEOUT = float(os.getenv("HTTP_POOL_TIMEOUT", "10.0"))
BOOTSTRAP_RETRIES = int(os.getenv("BOOTSTRAP_RETRIES", "5"))


def to_small_caps(text: str) -> str:
    small_caps_map = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ғ', 'g': 'ɢ', 'h': 'ʜ',
        'i': 'ɪ', 'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ', 'p': 'ᴘ',
        'q': 'ǫ', 'r': 'ʀ', 's': 's', 't': 'ᴛ', 'u': 'ᴜ', 'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x',
        'y': 'ʏ', 'z': 'ᴢ',
        'A': 'ᴀ', 'B': 'ʙ', 'C': 'ᴄ', 'D': 'ᴅ', 'E': 'ᴇ', 'F': 'ғ', 'G': 'ɢ', 'H': 'ʜ',
        'I': 'ɪ', 'J': 'ᴊ', 'K': 'ᴋ', 'L': 'ʟ', 'M': 'ᴍ', 'N': 'ɴ', 'O': 'ᴏ', 'P': 'ᴘ',
        'Q': 'ǫ', 'R': 'ʀ', 'S': 's', 'T': 'ᴛ', 'U': 'ᴜ', 'V': 'ᴠ', 'W': 'ᴡ', 'X': 'x',
        'Y': 'ʏ', 'Z': 'ᴢ'
    }
    return "".join(small_caps_map.get(c, c) for c in text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_help_text = (
        "WordSeek Commands List:\n\n"
        "• /start - Start the bot & show welcome message\n"
        "• /new [difficulty] - Start a new WordSeek challenge\n"
        "• /end - End current active game (Admins/Granted user)\n"
        "• /status - View ongoing game status & history\n"
        "• /score [user/reply] - Check game score & wins\n"
        "• /leaderboard [period] - View leaderboards\n"
        "• /difficulty - View difficulty settings\n"
        "• /seekoauth - Grant game end power to a member (Admin)\n"
        "• /rmseekoauth - Remove game end power from a member (Admin)\n"
        "• /help - Display this commands list\n\n"
        "Admin Commands:\n"
        "• /admin - Open admin panel\n"
        "• /stats - View total users, groups & uptime\n"
        "• /addscore <pts> [user/reply] - Add score to user\n"
        "• /rmscore <pts> [user/reply] - Remove score from user\n"
        "• /addadmin [user/reply] - Promote bot admin\n"
        "• /rmadmin [user/reply] - Demote bot admin\n"
        "• /ban [user/reply] - Ban user from bot\n"
        "• /unban [user/reply] - Unban user from bot\n"
        "• /broadcast <text/reply> - Broadcast message to all\n"
        "• /ubroadcast [pin] <text/reply> - Broadcast to user DMs\n"
        "• /gbroadcast [pin] <text/reply> - Broadcast to groups\n"
        "• /groups - List joined groups with direct join links"
    )
    small_caps_help = to_small_caps(raw_help_text)
    await update.effective_message.reply_text(small_caps_help)


async def extract_target_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[int | None, str | None]:
    msg = update.effective_message
    if not msg:
        return None, None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        name = f"@{u.username}" if u.username else u.first_name
        return u.id, name

    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("@"):
            if db_manager:
                u_data = db_manager.get_user_by_username(arg)
                if u_data:
                    return u_data.get("user_id"), f"@{u_data.get('username')}"
            return None, arg
        elif arg.isdigit() or (arg.startswith("-") and arg[1:].isdigit()):
            u_id = int(arg)
            if db_manager:
                u_data = db_manager.get_user_score(u_id)
                if u_data:
                    return u_id, f"@{u_data.get('username')}" if u_data.get('username') else str(u_id)
            return u_id, str(u_id)

    u = update.effective_user
    if u:
        name = f"@{u.username}" if u.username else u.first_name
        return u.id, name

    return None, None


async def seekoauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_group_admin(update, context):
        await update.effective_message.reply_text("🚨 Only group admins can grant game end permissions.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user's message or use `/seekoauth @username` / `/seekoauth <user_id>`", parse_mode='Markdown')
        return

    if db_manager:
        db_manager.grant_seek_oauth(update.effective_chat.id, target_id)
        await update.effective_message.reply_text(f"✅ Granted game end power to {target_name} in this group.")


async def rmseekoauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_group_admin(update, context):
        await update.effective_message.reply_text("🚨 Only group admins can revoke game end permissions.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user's message or use `/rmseekoauth @username` / `/rmseekoauth <user_id>`", parse_mode='Markdown')
        return

    if db_manager:
        db_manager.revoke_seek_oauth(update.effective_chat.id, target_id)
        await update.effective_message.reply_text(f"✅ Revoked game end power from {target_name} in this group.")


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        target_id = update.effective_user.id
        target_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name

    if not db_manager:
        await update.effective_message.reply_text("❌ Database error.")
        return

    user_data = db_manager.get_user_score(target_id)
    if not user_data:
        await update.effective_message.reply_text(f"📊 **Score for {target_name}:**\nNo game score recorded yet.", parse_mode='Markdown')
        return

    pts_global = user_data.get("points_global", 0)
    wins_global = user_data.get("wins_global", 0)
    pts_daily = user_data.get("points_daily", 0)
    pts_weekly = user_data.get("points_weekly", 0)
    pts_monthly = user_data.get("points_monthly", 0)

    msg = (
        f"📊 **WordSeek Score for {target_name}:**\n"
        f"-------------------------------------\n"
        f"🏆 **Global Points:** `{pts_global}`\n"
        f"🥇 **Global Wins:** `{wins_global}`\n"
        f"☀️ **Daily Points:** `{pts_daily}`\n"
        f"📅 **Weekly Points:** `{pts_weekly}`\n"
        f"🗓️ **Monthly Points:** `{pts_monthly}`"
    )
    await update.effective_message.reply_text(msg, parse_mode='Markdown')


async def ban_check_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and db_manager and db_manager.is_banned(user.id):
        if update.effective_message:
            try:
                await update.effective_message.reply_text("⛔ You are banned from using this bot.")
            except Exception:
                pass
        raise ApplicationHandlerStop


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to use this command.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user's message or specify `/ban @username` / `/ban <user_id>`", parse_mode='Markdown')
        return

    if target_id == ADMIN_USER_ID:
        await update.effective_message.reply_text("❌ You cannot ban the bot owner.")
        return

    if db_manager:
        db_manager.ban_user(target_id)
        await update.effective_message.reply_text(f"🚫 {target_name} (`{target_id}`) has been banned from the bot.", parse_mode='Markdown')


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to use this command.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user's message or specify `/unban @username` / `/unban <user_id>`", parse_mode='Markdown')
        return

    if db_manager:
        db_manager.unban_user(target_id)
        await update.effective_message.reply_text(f"✅ {target_name} (`{target_id}`) has been unbanned.", parse_mode='Markdown')


def get_uptime_str() -> str:
    seconds = int(time.time() - BOT_START_TIME)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner = (user.id == ADMIN_USER_ID)
    is_admin = (db_manager and db_manager.is_bot_admin(user.id))

    if not (is_owner or is_admin):
        await update.effective_message.reply_text("❌ You are not authorized to use the admin panel.")
        return

    msg = (
        "👑 **WordSeek Admin Panel**\n"
        "-------------------------------------\n"
        "• `/stats` - View bot total users, groups, and uptime\n"
        "• `/addscore <points> [user/reply]` - Add score to a user\n"
        "• `/rmscore <points> [user/reply]` - Remove score from a user\n"
        "• `/addadmin [user/reply]` - Add a bot admin (Owner only)\n"
        "• `/rmadmin [user/reply]` - Remove a bot admin (Owner only)\n"
        "• `/ban [user/reply]` - Ban user\n"
        "• `/unban [user/reply]` - Unban user\n"
        "• `/broadcast <text/reply>` - Broadcast message to all\n"
        "• `/ubroadcast [pin] <text/reply>` - Broadcast to DMs\n"
        "• `/gbroadcast [pin] <text/reply>` - Broadcast to groups\n"
        "• `/groups` - List joined groups with direct join links"
    )
    await update.effective_message.reply_text(msg, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to view bot stats.")
        return

    stats = db_manager.get_stats() if db_manager else {"total_users": 0, "total_groups": 0}
    uptime = get_uptime_str()

    msg = (
        "📈 **WordSeek Bot Statistics**\n"
        "-------------------------------------\n"
        f"👤 **Total Users:** `{stats['total_users']}`\n"
        f"👥 **Total Groups:** `{stats['total_groups']}`\n"
        f"⏱️ **Uptime:** `{uptime}`"
    )
    await update.effective_message.reply_text(msg, parse_mode='Markdown')


async def addscore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: `/addscore <points> [user/reply]`", parse_mode='Markdown')
        return

    try:
        points = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Points must be a valid integer.")
        return

    context.args = context.args[1:]
    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Target user not found. Usage: Reply to message or `/addscore <points> @username`")
        return

    if db_manager:
        db_manager.add_score(target_id, target_name, points)
        await update.effective_message.reply_text(f"✅ Added **{points}** points to {target_name} (`{target_id}`).", parse_mode='Markdown')


async def rmscore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to use this command.")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: `/rmscore <points> [user/reply]`", parse_mode='Markdown')
        return

    try:
        points = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Points must be a valid integer.")
        return

    context.args = context.args[1:]
    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Target user not found. Usage: Reply to message or `/rmscore <points> @username`")
        return

    if db_manager:
        db_manager.remove_score(target_id, points)
        await update.effective_message.reply_text(f"✅ Removed **{points}** points from {target_name} (`{target_id}`).", parse_mode='Markdown')


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        await update.effective_message.reply_text("❌ Only the bot owner can add admins.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user or `/addadmin @username` / `/addadmin <user_id>`", parse_mode='Markdown')
        return

    if db_manager:
        db_manager.add_bot_admin(target_id)
        await update.effective_message.reply_text(f"👑 Added {target_name} (`{target_id}`) as bot admin.", parse_mode='Markdown')


async def rmadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        await update.effective_message.reply_text("❌ Only the bot owner can remove admins.")
        return

    target_id, target_name = await extract_target_user_info(update, context)
    if not target_id:
        await update.effective_message.reply_text("⚠️ Usage: Reply to a user or `/rmadmin @username` / `/rmadmin <user_id>`", parse_mode='Markdown')
        return

    if db_manager:
        db_manager.remove_bot_admin(target_id)
        await update.effective_message.reply_text(f"✅ Removed {target_name} (`{target_id}`) from bot admins.", parse_mode='Markdown')


async def _send_broadcast_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_to_forward, text_to_send: str | None, should_pin: bool) -> bool:
    try:
        if message_to_forward:
            sent_msg = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=message_to_forward.chat_id,
                message_id=message_to_forward.message_id
            )
        else:
            sent_msg = await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode='Markdown')

        if should_pin and sent_msg:
            try:
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id)
            except Exception as e:
                logger.warning(f"Could not pin message in {chat_id}: {e}")
        return True
    except error.Forbidden:
        return False
    except Exception as e:
        logger.error(f"Failed broadcast to {chat_id}: {e}")
        return False


async def ubroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to broadcast.")
        return

    should_pin = False
    args = list(context.args) if context.args else []
    if args and args[0].lower() == "pin":
        should_pin = True
        args.pop(0)

    reply_msg = update.effective_message.reply_to_message
    text_to_send = " ".join(args) if args else None

    if not reply_msg and not text_to_send:
        await update.effective_message.reply_text("⚠️ Usage: `/ubroadcast [pin] <text>` or reply to a message with `/ubroadcast [pin]`", parse_mode='Markdown')
        return

    if not db_manager:
        await update.effective_message.reply_text("❌ Database error.")
        return

    dm_chats = db_manager.get_chats_by_type(["PRIVATE", "private"])
    user_ids = set()
    for c in dm_chats:
        user_ids.add(c["chat_id"])
    for u_id in db_manager.data.get("started_users", []):
        user_ids.add(u_id)

    status_msg = await update.effective_message.reply_text(f"📢 *Broadcasting to {len(user_ids)} user DMs...*", parse_mode='Markdown')

    success_count = 0
    fail_count = 0

    for u_id in user_ids:
        res = await _send_broadcast_message(context, u_id, reply_msg, text_to_send, should_pin)
        if res:
            success_count += 1
        else:
            fail_count += 1

    pin_str = "\n📌 *Messages Pinned*" if should_pin else ""
    await status_msg.edit_text(
        f"✅ **DM Broadcast Complete**\nSuccessful: **{success_count}**\nFailed: **{fail_count}**{pin_str}",
        parse_mode='Markdown'
    )


async def gbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to broadcast.")
        return

    should_pin = False
    args = list(context.args) if context.args else []
    if args and args[0].lower() == "pin":
        should_pin = True
        args.pop(0)

    reply_msg = update.effective_message.reply_to_message
    text_to_send = " ".join(args) if args else None

    if not reply_msg and not text_to_send:
        await update.effective_message.reply_text("⚠️ Usage: `/gbroadcast [pin] <text>` or reply to a message with `/gbroadcast [pin]`", parse_mode='Markdown')
        return

    if not db_manager:
        await update.effective_message.reply_text("❌ Database error.")
        return

    group_chats = db_manager.get_chats_by_type(["GROUP", "SUPERGROUP", "group", "supergroup"])
    group_ids = [c["chat_id"] for c in group_chats]

    status_msg = await update.effective_message.reply_text(f"📢 *Broadcasting to {len(group_ids)} groups...*", parse_mode='Markdown')

    success_count = 0
    fail_count = 0

    for g_id in group_ids:
        res = await _send_broadcast_message(context, g_id, reply_msg, text_to_send, should_pin)
        if res:
            success_count += 1
        else:
            fail_count += 1

    pin_str = "\n📌 *Messages Pinned*" if should_pin else ""
    await status_msg.edit_text(
        f"✅ **Group Broadcast Complete**\nSuccessful: **{success_count}**\nFailed: **{fail_count}**{pin_str}",
        parse_mode='Markdown'
    )


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    is_owner_or_admin = (user.id == ADMIN_USER_ID) or (db_manager and db_manager.is_bot_admin(user.id))
    if not is_owner_or_admin:
        await update.effective_message.reply_text("❌ You are not authorized to view the groups list.")
        return

    await render_groups_page(update, context, page=0)


async def render_groups_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not db_manager:
        return

    groups = db_manager.get_chats_by_type(["GROUP", "SUPERGROUP", "group", "supergroup"])
    total_groups = len(groups)

    if total_groups == 0:
        msg = "👥 **Joined Groups List:**\n\n*The bot is currently not in any recorded groups.*"
        await safe_edit_message(update, msg, parse_mode="Markdown")
        return
    per_page = 50
    total_pages = (total_groups + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total_groups)
    page_groups = groups[start_idx:end_idx]

    lines = [f"👥 **Joined Groups List (Page {page + 1}/{total_pages}):**", "-------------------------------------"]

    for i, g in enumerate(page_groups, start=start_idx + 1):
        title = g.get("title") or f"Group {g['chat_id']}"
        link = g.get("invite_link")
        if link:
            lines.append(f"{i}. [{title}]({link}) (`{g['chat_id']}`)")
        else:
            lines.append(f"{i}. **{title}** (`{g['chat_id']}`)")

    msg_text = "\n".join(lines)

    buttons = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"groups_page_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"groups_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    await safe_edit_message(update, msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)


async def track_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if not result:
        return

    chat = result.chat
    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    was_member = result.old_chat_member.status in ['member', 'administrator']
    is_member = result.new_chat_member.status in ['member', 'administrator']

    user_who_did = result.from_user
    user_str = f"@{user_who_did.username}" if user_who_did and user_who_did.username else (user_who_did.first_name if user_who_did else "Unknown")

    chat_photo = None
    try:
        chat_full = await context.bot.get_chat(chat.id)
        chat_photo = chat_full.photo
    except Exception as e:
        logger.warning(f"Could not fetch chat photo for {chat.id}: {e}")

    invite_link = ""
    if is_member and not was_member:
        try:
            invite_link = await context.bot.export_chat_invite_link(chat.id)
        except Exception:
            invite_link = chat.invite_link or ""

        if db_manager:
            db_manager.add_chat(chat.id, chat.type.name, chat.title, invite_link, update.effective_message.date.timestamp() if update.effective_message else time.time())

        log_msg = (
            f"➕ **Bot Added to Group!**\n"
            f"-------------------------------------\n"
            f"📛 **Group Name:** {chat.title}\n"
            f"🆔 **Group ID:** `{chat.id}`\n"
            f"👤 **Added By:** {user_str} (`{user_who_did.id if user_who_did else 'N/A'}`)\n"
            f"🔗 **Invite Link:** {invite_link or 'N/A'}"
        )

    elif was_member and not is_member:
        if db_manager:
            db_manager.remove_chat(chat.id)

        log_msg = (
            f"➖ **Bot Removed from Group!**\n"
            f"-------------------------------------\n"
            f"📛 **Group Name:** {chat.title}\n"
            f"🆔 **Group ID:** `{chat.id}`\n"
            f"👤 **Removed By:** {user_str} (`{user_who_did.id if user_who_did else 'N/A'}`)"
        )
    else:
        return

    if LOG_CHANNEL_ID:
        try:
            if chat_photo:
                file_id = chat_photo.big_file_id
                await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=file_id, caption=log_msg, parse_mode='Markdown')
            else:
                await send_log_notification(context, log_msg)
        except Exception as e:
            logger.error(f"Failed to send group log notification: {e}")
            await send_log_notification(context, log_msg)


def create_application(token: str = BOT_TOKEN) -> Application | None:
    if not token:
        return None

    request = HTTPXRequest(
        connect_timeout=HTTP_CONNECT_TIMEOUT,
        read_timeout=HTTP_READ_TIMEOUT,
        write_timeout=HTTP_WRITE_TIMEOUT,
        pool_timeout=HTTP_POOL_TIMEOUT,
        http_version="1.1"
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=HTTP_CONNECT_TIMEOUT,
        read_timeout=HTTP_READ_TIMEOUT,
        write_timeout=HTTP_WRITE_TIMEOUT,
        pool_timeout=HTTP_POOL_TIMEOUT,
        http_version="1.1"
    )

    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )

    # 0. Ban check middleware (group=-1)
    app.add_handler(MessageHandler(filters.ALL, ban_check_middleware), group=-1)

    # 1. Register Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("new", new_game_command))
    app.add_handler(CommandHandler("end", end_game_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("score", score_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("difficulty", difficulty_command))
    app.add_handler(CommandHandler("seekoauth", seekoauth_command))
    app.add_handler(CommandHandler("rmseekoauth", rmseekoauth_command))
    app.add_handler(CommandHandler("help", help_command))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addscore", addscore_command))
    app.add_handler(CommandHandler("rmscore", rmscore_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("rmadmin", rmadmin_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("ubroadcast", ubroadcast_command))
    app.add_handler(CommandHandler("gbroadcast", gbroadcast_command))
    app.add_handler(CommandHandler("groups", groups_command))

    # 2. Group Member Updates (bot join/leave)
    app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # 3. Callback query handler for inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # 4. Message handler for guesses
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r'^[a-zA-Z]{1,8}$'), 
            handle_guess
        )
    )

    return app

def main():
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("FATAL ERROR: BOT_TOKEN not found. Please set it in the .env file.")
        return

    application = create_application(BOT_TOKEN)

    logger.info("🚀 WordRush Bot is running (Guess Uniqueness & Leaderboards Ready)...")
    
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=BOOTSTRAP_RETRIES
    )

if __name__ == "__main__":
    main()
