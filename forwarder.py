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
# Ensure essential environment variables are set before starting.
if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise RuntimeError(
        "CRITICAL ERROR: API_ID, API_HASH, and BOT_TOKEN must be set in your .env file."
    )

# --- DATABASE SETUP ---
DB_FILE = 'tasks.db'

def init_db():
    """Initializes the SQLite database and the tasks table."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            destination_id INTEGER NOT NULL,
            only_replies BOOLEAN NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# --- TELETHON CLIENT (THE ENGINE) ---
# This part logs into your user account to listen for and forward messages.
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    """Listens for new messages and forwards them based on tasks in the database."""
    # Uncomment the line below for future debugging if needed.
    # print(f"DEBUG: New message received from chat ID: {event.chat_id}")
    chat_id = event.chat_id
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT destination_id, only_replies FROM tasks WHERE source_id = ?", (chat_id,))
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        return

    message = event.message
    
    for destination_id, only_replies in tasks:
        if only_replies and not message.is_reply:
            print(f"Skipping message {message.id} from {chat_id}: not a reply.")
            continue

        print(f"Forwarding message {message.id} from {chat_id} to {destination_id}")
        
        try:
            # **THE FIX IS HERE:**
            # We explicitly send the message text and media file instead of the whole message object.
            # This creates a new, clean message and avoids potential metadata/permission conflicts
            # even when you are the owner of the channel.
            await client.send_message(
                entity=destination_id,
                message=message.text,
                file=message.media,
                reply_to=message.reply_to_msg_id if message.is_reply else None
            )
        except Exception as e:
            print(f"Could not forward message {message.id}. Error: {e}")


# --- TELEGRAM BOT (THE INTERFACE) ---
# This part handles commands from you to manage the forwarding tasks.

# Conversation states for the /newtask command
SOURCE, DESTINATION, FILTER_REPLY, CONFIRMATION = range(4)

def get_chat_id_from_update(update: Update, context: CallbackContext) -> bool:
    """Helper function to extract chat ID from a forwarded message or text."""
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
    update.message.reply_text(
        "Welcome to the Auto Forwarder Bot!\n\n"
        "Use /newtask to set up a new forwarding rule.\n"
        "Use /tasks to see all active tasks.\n"
        "Use /delete to remove a task."
    )

def new_task_start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text(
        "Let's set up a new task.\n"
        "First, define the Source chat.\n\n"
        "You can either:\n"
        "1. **Forward a message** from the source channel/group/user/bot.\n"
        "2. Send me the **numeric Chat ID** of the source."
    )
    return SOURCE

def get_source(update: Update, context: CallbackContext) -> int:
    if not get_chat_id_from_update(update, context):
        return SOURCE
        
    context.user_data['source_id'] = context.user_data['chat_id']
    context.user_data['source_title'] = context.user_data['chat_title']
    
    update.message.reply_text(
        f"✅ Source set to: **{context.user_data['source_title']}** (`{context.user_data['source_id']}`)\n\n"
        "Now, send me the **Destination** (where to forward to).",
        parse_mode='Markdown'
    )
    return DESTINATION

def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id_from_update(update, context):
        return DESTINATION

    context.user_data['destination_id'] = context.user_data['chat_id']
    context.user_data['destination_title'] = context.user_data['chat_title']

    update.message.reply_text(
        f"✅ Destination set to: **{context.user_data['destination_title']}** (`{context.user_data['destination_id']}`)\n\n"
        "Do you want to forward **only replies** to other messages?",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([['Yes', 'No']], one_time_keyboard=True, resize_keyboard=True)
    )
    return FILTER_REPLY

def get_filter_reply(update: Update, context: CallbackContext) -> int:
    text = update.message.text.lower()
    if text not in ['yes', 'no']:
        update.message.reply_text("Please choose 'Yes' or 'No'.")
        return FILTER_REPLY

    context.user_data['only_replies'] = (text == 'yes')
    only_replies_text = "Yes" if context.user_data['only_replies'] else "No"
    
    summary = (
        f"Please confirm your new task:\n\n"
        f"➡️ **From:** {context.user_data['source_title']} (`{context.user_data['source_id']}`)\n"
        f"↘️ **To:** {context.user_data['destination_title']} (`{context.user_data['destination_id']}`)\n"
        f"💬 **Only Forward Replies:** {only_replies_text}\n\n"
        "Is this correct?"
    )
    update.message.reply_text(
        summary,
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True, resize_keyboard=True)
    )
    return CONFIRMATION

def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("Task cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (source_id, destination_id, only_replies) VALUES (?, ?, ?)",
        (context.user_data['source_id'], context.user_data['destination_id'], context.user_data['only_replies'])
    )
    conn.commit()
    conn.close()
    
    update.message.reply_text("✅ Task saved successfully!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def list_tasks(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, source_id, destination_id, only_replies FROM tasks")
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        update.message.reply_text("You have no active forwarding tasks.")
        return

    message = "Your active tasks:\n\n"
    for task_id, source, dest, replies in tasks:
        reply_text = "Yes" if replies else "No"
        message += (
            f"🔹 **Task ID:** {task_id}\n"
            f"   **From:** `{source}`\n"
            f"   **To:** `{dest}`\n"
            f"   **Only Replies:** {reply_text}\n\n"
        )
    update.message.reply_text(message, parse_mode='Markdown')

def delete_task_start(update: Update, context: CallbackContext) -> int:
    list_tasks(update, context)
    update.message.reply_text("Please send the Task ID of the task you want to delete.")
    return 0

def delete_task_confirm(update: Update, context: CallbackContext) -> int:
    try:
        task_id = int(update.message.text)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        
        if cursor.rowcount > 0:
            update.message.reply_text(f"✅ Task {task_id} has been deleted.")
        else:
            update.message.reply_text(f"❌ Task {task_id} was not found.")
        
        conn.close()
    except ValueError:
        update.message.reply_text("Invalid ID. Please send a number.")

    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('Operation cancelled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    """Main function to set up and run both the bot and the client."""
    init_db()
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)],
            DESTINATION: [MessageHandler(Filters.all & ~Filters.command, get_destination)],
            FILTER_REPLY: [MessageHandler(Filters.regex('^(Yes|No)$'), get_filter_reply)],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
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