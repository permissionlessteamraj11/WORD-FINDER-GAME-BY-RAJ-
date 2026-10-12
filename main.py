import os
import random
import re
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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
         
# --- JSON DB Manager Class ---
class JSONDBManager:
    """Handles all interactions with a local JSON file database."""
    def __init__(self, db_path: str = "database.json"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            if not os.path.exists(self.db_path):
                initial_data = {
                    "leaderboard": {},
                    "active_games": {},
                    "known_chats": {}
                }
                self._write_db_unlocked(initial_data)
                logger.info(f"✅ Initialized JSON database at {self.db_path}")

    def _read_db_unlocked(self) -> dict:
        try:
            if not os.path.exists(self.db_path):
                return {"leaderboard": {}, "active_games": {}, "known_chats": {}}
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("leaderboard", {})
                data.setdefault("active_games", {})
                data.setdefault("known_chats", {})
                return data
        except Exception as e:
            logger.error(f"Error reading database file {self.db_path}: {e}")
            return {"leaderboard": {}, "active_games": {}, "known_chats": {}}

    def _write_db_unlocked(self, data: dict):
        try:
            temp_path = f"{self.db_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.db_path)
        except Exception as e:
            logger.error(f"Error writing database file {self.db_path}: {e}")

    def update_leaderboard(self, user_id: int, username: str, points_to_add: int):
        user_key = str(user_id)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        with self._lock:
            data = self._read_db_unlocked()
            leaderboard = data.setdefault("leaderboard", {})
            user_data = leaderboard.get(user_key, {
                "user_id": user_id,
                "username": username,
                "points_global": 0,
                "wins_global": 0,
                "points_daily": 0,
                "wins_daily": 0,
                "last_win_date_daily": None,
                "points_weekly": 0,
                "wins_weekly": 0,
                "last_win_date_weekly": None,
                "points_monthly": 0,
                "wins_monthly": 0,
                "last_win_date_monthly": None
            })

            user_data["username"] = username
            user_data["points_global"] = user_data.get("points_global", 0) + points_to_add
            user_data["wins_global"] = user_data.get("wins_global", 0) + 1

            periods_config = [
                ('daily', timedelta(days=1)),
                ('weekly', timedelta(weeks=1)),
                ('monthly', timedelta(days=30))
            ]

            for period, threshold in periods_config:
                last_win_str = user_data.get(f"last_win_date_{period}")
                need_reset = False
                if last_win_str:
                    try:
                        last_win_dt = datetime.fromisoformat(last_win_str)
                        if last_win_dt.tzinfo is None:
                            last_win_dt = last_win_dt.replace(tzinfo=timezone.utc)
                        if now - last_win_dt >= threshold:
                            need_reset = True
                    except (ValueError, TypeError):
                        need_reset = True

                if need_reset or not last_win_str:
                    user_data[f"points_{period}"] = points_to_add
                    user_data[f"wins_{period}"] = 1
                else:
                    user_data[f"points_{period}"] = user_data.get(f"points_{period}", 0) + points_to_add
                    user_data[f"wins_{period}"] = user_data.get(f"wins_{period}", 0) + 1

                user_data[f"last_win_date_{period}"] = now_iso

            leaderboard[user_key] = user_data
            self._write_db_unlocked(data)

    def get_leaderboard_data(self, period: str, limit=10) -> List[Tuple[str, int, int]]:
        points_key = f'points_{period}'
        wins_key = f'wins_{period}'
        now = datetime.now(timezone.utc)
        thresholds = {
            'daily': timedelta(days=1),
            'weekly': timedelta(weeks=1),
            'monthly': timedelta(days=30)
        }

        with self._lock:
            data = self._read_db_unlocked()
            leaderboard = data.get("leaderboard", {})

            result = []
            for user_key, doc in leaderboard.items():
                if period in thresholds:
                    last_win_str = doc.get(f"last_win_date_{period}")
                    if last_win_str:
                        try:
                            last_win_dt = datetime.fromisoformat(last_win_str)
                            if last_win_dt.tzinfo is None:
                                last_win_dt = last_win_dt.replace(tzinfo=timezone.utc)
                            if now - last_win_dt >= thresholds[period]:
                                points = 0
                                wins = 0
                            else:
                                points = doc.get(points_key, 0)
                                wins = doc.get(wins_key, 0)
                        except (ValueError, TypeError):
                            points = 0
                            wins = 0
                    else:
                        points = 0
                        wins = 0
                else:
                    points = doc.get(points_key, 0)
                    wins = doc.get(wins_key, 0)

                if points > 0 or period == 'global':
                    result.append((doc.get('username'), points, wins))

            result.sort(key=lambda x: x[1], reverse=True)
            return result[:limit]

    def get_game_state(self, chat_id: int) -> Dict | None:
        chat_key = str(chat_id)
        with self._lock:
            data = self._read_db_unlocked()
            return data.get("active_games", {}).get(chat_key)

    def save_game_state(self, chat_id: int, state: Dict):
        chat_key = str(chat_id)
        state_to_save = {'chat_id': chat_id, **state}
        with self._lock:
            data = self._read_db_unlocked()
            data.setdefault("active_games", {})[chat_key] = state_to_save
            self._write_db_unlocked(data)

    def delete_game_state(self, chat_id: int):
        chat_key = str(chat_id)
        with self._lock:
            data = self._read_db_unlocked()
            if chat_key in data.get("active_games", {}):
                del data["active_games"][chat_key]
                self._write_db_unlocked(data)

    def add_chat(self, chat_id: int, chat_type: str, date: float):
        chat_key = str(chat_id)
        with self._lock:
            data = self._read_db_unlocked()
            data.setdefault("known_chats", {})[chat_key] = {
                'chat_id': chat_id,
                'chat_type': chat_type,
                'last_active': date
            }
            self._write_db_unlocked(data)

    def get_all_chat_ids(self) -> List[int]:
        with self._lock:
            data = self._read_db_unlocked()
            chats = data.get("known_chats", {})
            return [doc['chat_id'] for doc in chats.values() if 'chat_id' in doc]

# --- Initialize DB Manager ---
db_manager = None
try:
    db_manager = JSONDBManager(JSON_DB_PATH)
except Exception as e:
    logger.error(f"❌ FATAL: Could not initialize JSON database. Error: {e}")
    db_manager = None

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

def get_start_keyboard():
    keyboard = [
        [InlineKeyboardButton("❓ Help & Info", callback_data="show_help_menu")],
        [
            InlineKeyboardButton("💬 Report Bugs", url="https://t.me/Onlymrabhi01"), 
            InlineKeyboardButton("📢 Updates Channel", url="https://t.me/narzob") 
        ],
        [InlineKeyboardButton("➕ Add Bot to Group", url="https://t.me/narzowordseekbot?startgroup=true")]
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

# --- Leaderboard Utility Function (Unchanged) ---

async def display_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    """Fetches and displays the leaderboard for the given period."""
    if not db_manager:
        await (update.callback_query.edit_message_text if update.callback_query else update.message.reply_text)("❌ *Database Error*. Cannot fetch leaderboard.")
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
        await update.callback_query.edit_message_text(message, reply_markup=get_leaderboard_menu_keyboard(), parse_mode='Markdown')
    else:
        await update.message.reply_text(message, reply_markup=get_leaderboard_menu_keyboard(), parse_mode='Markdown')

# --- Command Handlers (All Unchanged) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if db_manager and update.effective_message:
        db_manager.add_chat(update.effective_chat.id, update.effective_chat.type.name, update.effective_message.date.timestamp())
    
    user = update.effective_user
    chat = update.effective_chat
    user_info = f"@{user.username}" if user.username else f"{user.first_name} (ID: `{user.id}`)"
    log_msg = f"🚀 **Bot Started**\n👤 **User:** {user_info}\n💬 **Chat:** {chat.title or 'Private'} (`{chat.id}`)"
    await send_log_notification(context, log_msg)

    # Stylish Start Message
    await update.message.reply_text(
        "👋 *Hello! I'm* **@narzowordseekbot** 🤖\n"
        "-------------------------------------\n"
        "The **Ultimate Word Challenge** on Telegram!\n\n"
        "📜 **Goal:** *Guess the secret word using hints (🟩/🟨/🟥).*\n"
        "🏆 **Compete:** *Win to earn points and climb the Global Leaderboard!* 🌐\n\n"
        "👉 Tap **/new** or the button below to start your rush!\n"
        "-------------------------------------",
        reply_markup=get_start_keyboard(),
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
    
    if not db_manager or not db_manager.get_game_state(chat_id):
        await update.message.reply_text("❌ *No game is currently running to end*.")
        return
        
    if not await is_group_admin(update, context):
        await update.message.reply_text("🚨 *Admin Check Failed*. You must be an **Admin** to force-end the game.", parse_mode='Markdown')
        return

    game_state = db_manager.get_game_state(chat_id)
    word = game_state.get('word', 'UNKNOWN')
    db_manager.delete_game_state(chat_id)
    
    user = update.effective_user
    user_info = f"@{user.username}" if user and user.username else (user.first_name if user else "User")
    log_msg = f"🛑 **Game Ended Manually**\n👤 **By:** {user_info}\n💬 **Chat ID:** `{chat_id}`\n🔑 **Word Was:** `{word}`"
    await send_log_notification(context, log_msg)

    await update.message.reply_text(
        f"🛑 **Game Ended!**\n"
        f"*The secret word was:* **`{word}`**.", 
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
        await query.edit_message_text(
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
        await query.edit_message_text(
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
        await query.edit_message_text(commands_list, reply_markup=get_help_menu_keyboard(), parse_mode='Markdown')

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
        await query.edit_message_text(commands_list, reply_markup=get_help_menu_keyboard(), parse_mode='Markdown')
        
    elif query.data == "show_leaderboard_menu":
        await query.edit_message_text(
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
        await query.edit_message_text(
            "🎯 **Select Your Challenge Level:**\n"
            "*Choose the word length and point value.*",
            reply_markup=get_new_game_keyboard(),
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("start_"):
        difficulty = query.data.split('_')[1]
        
        if db_manager and db_manager.get_game_state(chat_id):
            await query.edit_message_text("⏳ *A game is already active*. Use **/end** to stop it first.")
            return

        success, message = await start_new_game_logic(chat_id, difficulty)
        if success:
            user = update.effective_user
            user_info = f"@{user.username}" if user and user.username else (user.first_name if user else "User")
            log_msg = f"🎮 **New Game Started (Button)**\n🎯 **Difficulty:** {difficulty.capitalize()}\n👤 **Started By:** {user_info}\n💬 **Chat ID:** `{chat_id}`"
            await send_log_notification(context, log_msg)
            await query.edit_message_text(message, parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ *Game start failed*: {message}", parse_mode='Markdown')

# --- Updated Guess Handler (Handles the new error message) ---

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

def main():
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("FATAL ERROR: BOT_TOKEN not found. Please set it in the .env file.")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()

    # Register Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("new", new_game_command))
    application.add_handler(CommandHandler("end", end_game_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("difficulty", difficulty_command)) 
    application.add_handler(CommandHandler("status", status_command)) 
    
    if ADMIN_USER_ID != 0 and db_manager is not None:
        application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    application.add_handler(CommandHandler("help", start_command)) 

    # Callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Message handler for guesses:
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r'^[a-zA-Z]{1,8}$'), 
            handle_guess
        )
    )

    logger.info("🚀 WordRush Bot is running (Guess Uniqueness & Leaderboards Ready)...")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
