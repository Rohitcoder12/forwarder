import sqlite3
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext,
)

# Load environment variables from a .env file
load_dotenv()

# --- CONFIGURATION (loaded from .env file) ---
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
SESSION_NAME = 'telegram_forwarder'

# --- SANITY CHECK ---
if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise RuntimeError(
        "CRITICAL ERROR: API_ID, API_HASH, and BOT_TOKEN must be set in your .env file."
    )

# --- DATABASE SETUP ---
DB_FILE = 'tasks.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # NEW: Added blacklist_words and whitelist_words columns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            destination_id INTEGER NOT NULL,
            only_replies BOOLEAN NOT NULL DEFAULT 0,
            blacklist_words TEXT,
            whitelist_words TEXT
        )
    ''')
    conn.commit()
    conn.close()

# --- TELETHON CLIENT (THE ENGINE) ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    chat_id = event.chat_id
    message = event.message
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # NEW: Fetching the new filter columns
    cursor.execute(
        "SELECT destination_id, only_replies, blacklist_words, whitelist_words FROM tasks WHERE source_id = ?", 
        (chat_id,)
    )
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        return

    full_text = (message.text or "").lower()

    for destination_id, only_replies, blacklist, whitelist in tasks:
        # Filter for replies (your use case will be 'No')
        if only_replies and not message.is_reply:
            continue

        # NEW: Whitelist Filter Logic
        if whitelist:
            whitelist_words = [word.strip() for word in whitelist.lower().split(',')]
            if not any(word in full_text for word in whitelist_words):
                print(f"Skipping message {message.id}: No whitelist words found.")
                continue

        # NEW: Blacklist Filter Logic
        if blacklist:
            blacklist_words = [word.strip() for word in blacklist.lower().split(',')]
            if any(word in full_text for word in blacklist_words):
                print(f"Skipping message {message.id}: A blacklist word was found.")
                continue
        
        print(f"Forwarding message {message.id} from {chat_id} to {destination_id}")
        
        downloaded_file_path = None
        try:
            if message.media:
                downloaded_file_path = await message.download_media()
                await client.send_file(
                    entity=destination_id,
                    file=downloaded_file_path,
                    caption=message.text,
                    reply_to=message.reply_to_msg_id if message.is_reply else None
                )
            elif message.text:
                await client.send_message(
                    entity=destination_id,
                    message=message.text,
                    reply_to=message.reply_to_msg_id if message.is_reply else None
                )
            print("Message forwarded successfully.")

        except Exception as e:
            print(f"Could not forward message {message.id}. Error: {e}")
        finally:
            if downloaded_file_path and os.path.exists(downloaded_file_path):
                os.remove(downloaded_file_path)

# --- TELEGRAM BOT (THE INTERFACE) ---

# NEW: Updated conversation states
SOURCE, DESTINATION, FILTER_REPLY, BLACKLIST, WHITELIST, CONFIRMATION = range(6)

def get_chat_id_from_update(update: Update, context: CallbackContext) -> bool:
    # This function is unchanged
    if update.message.forward_from_chat:
        context.user_data['chat_id'] = update.message.forward_from_chat.id
        context.user_data['chat_title'] = update.message.forward_from_chat.title or "N/A"
    elif update.message.forward_from:
        context.user_data['chat_id'] = update.message.forward_from.id
        context.user_data['chat_title'] = update.message.forward_from.first_name
    else:
        try:
            context.user_data['chat_id'] = int(update.message.text)
            context.user_data['chat_title'] = f"ID: {update.message.text}"
        except (ValueError, TypeError):
            update.message.reply_text("Invalid input. Please forward a message or send the numeric ID.")
            return False
    return True

def start(update: Update, context: CallbackContext) -> None:
    # This function is unchanged
    update.message.reply_text("Welcome! Use /newtask to set up forwarding, /tasks to view, and /delete to remove.")

def new_task_start(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    update.message.reply_text("Let's set up a new task. First, define the Source chat.")
    return SOURCE

def get_source(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    if not get_chat_id_from_update(update, context): return SOURCE
    context.user_data['source_id'] = context.user_data['chat_id']
    context.user_data['source_title'] = context.user_data['chat_title']
    update.message.reply_text(f"✅ Source set to: **{context.user_data['source_title']}**\n\nNow, send the **Destination**.", parse_mode='Markdown')
    return DESTINATION

def get_destination(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    if not get_chat_id_from_update(update, context): return DESTINATION
    context.user_data['destination_id'] = context.user_data['chat_id']
    context.user_data['destination_title'] = context.user_data['chat_title']
    update.message.reply_text(f"✅ Destination set.\n\nForward **only replies**?", reply_markup=ReplyKeyboardMarkup([['Yes', 'No']], one_time_keyboard=True))
    return FILTER_REPLY

def get_filter_reply(update: Update, context: CallbackContext) -> int:
    # NEW: Transitions to BLACKLIST instead of CONFIRMATION
    context.user_data['only_replies'] = (update.message.text.lower() == 'yes')
    update.message.reply_text("Now, let's set a **Blacklist**.\nSend words separated by a comma (e.g., `spam,crypto,scam`). If a message contains any of these, it will be ignored.\n\nSend /skip to not use a blacklist.", reply_markup=ReplyKeyboardRemove())
    return BLACKLIST

def get_blacklist(update: Update, context: CallbackContext) -> int:
    # NEW function to handle blacklist words
    if update.message.text.lower() == '/skip':
        context.user_data['blacklist'] = None
    else:
        context.user_data['blacklist'] = update.message.text
    update.message.reply_text("✅ Blacklist set.\n\nNow, let's set a **Whitelist**.\nSend words separated by a comma. A message will ONLY be forwarded if it contains one of these words.\n\nSend /skip to not use a whitelist.")
    return WHITELIST

def get_whitelist(update: Update, context: CallbackContext) -> int:
    # NEW function to handle whitelist words and show final confirmation
    if update.message.text.lower() == '/skip':
        context.user_data['whitelist'] = None
    else:
        context.user_data['whitelist'] = update.message.text

    # Build the summary text
    summary = (
        f"Please confirm your new task:\n\n"
        f"➡️ **From:** {context.user_data['source_title']} (`{context.user_data['source_id']}`)\n"
        f"↘️ **To:** {context.user_data['destination_title']} (`{context.user_data['destination_id']}`)\n"
        f"💬 **Only Replies:** {'Yes' if context.user_data['only_replies'] else 'No'}\n"
        f"🚫 **Blacklist:** `{context.user_data['blacklist'] or 'Not set'}`\n"
        f"✅ **Whitelist:** `{context.user_data['whitelist'] or 'Not set'}`\n\n"
        "Is this correct?"
    )
    update.message.reply_text(summary, parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True))
    return CONFIRMATION

def save_task(update: Update, context: CallbackContext) -> int:
    # NEW: Saves the new filter data to the database
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("Task cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (source_id, destination_id, only_replies, blacklist_words, whitelist_words) VALUES (?, ?, ?, ?, ?)",
        (
            context.user_data['source_id'], 
            context.user_data['destination_id'], 
            context.user_data['only_replies'],
            context.user_data['blacklist'],
            context.user_data['whitelist']
        )
    )
    conn.commit()
    conn.close()
    
    update.message.reply_text("✅ Task saved successfully!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def list_tasks(update: Update, context: CallbackContext) -> None:
    # NEW: Displays the filter words for each task
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, source_id, destination_id, blacklist_words, whitelist_words FROM tasks")
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        update.message.reply_text("You have no active tasks.")
        return

    message = "Your active tasks:\n\n"
    for task_id, source, dest, blacklist, whitelist in tasks:
        message += (
            f"🔹 **Task ID:** {task_id}\n"
            f"   **From:** `{source}`\n"
            f"   **To:** `{dest}`\n"
            f"   **Blacklist:** `{blacklist or 'None'}`\n"
            f"   **Whitelist:** `{whitelist or 'None'}`\n\n"
        )
    update.message.reply_text(message, parse_mode='Markdown')

def delete_task_start(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    list_tasks(update, context)
    update.message.reply_text("Please send the Task ID you want to delete.")
    return 0

def delete_task_confirm(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    try:
        task_id = int(update.message.text)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        if cursor.rowcount > 0: update.message.reply_text(f"✅ Task {task_id} has been deleted.")
        else: update.message.reply_text(f"❌ Task {task_id} not found.")
        conn.close()
    except ValueError:
        update.message.reply_text("Invalid ID. Please send a number.")
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    # This function is unchanged
    update.message.reply_text('Operation cancelled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    init_db()
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher

    # NEW: Updated ConversationHandler with new states and entry points
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)],
            DESTINATION: [MessageHandler(Filters.all & ~Filters.command, get_destination)],
            FILTER_REPLY: [MessageHandler(Filters.regex('^(Yes|No)$'), get_filter_reply)],
            BLACKLIST: [MessageHandler(Filters.text, get_blacklist)],
            WHITELIST: [MessageHandler(Filters.text, get_whitelist)],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # ... Rest of main function is unchanged ...
    delete_handler = ConversationHandler(
        entry_points=[CommandHandler('delete', delete_task_start)],
        states={0: [MessageHandler(Filters.text & ~Filters.command, delete_task_confirm)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("tasks", list_tasks))
    dp.add_handler(conv_handler)
    dp.add_handler(delete_handler)
    updater.start_polling()
    print("Control Bot started...")
    await client.start()
    print("Telethon client (user account) started...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped gracefully.")