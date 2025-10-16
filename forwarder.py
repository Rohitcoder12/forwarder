import sqlite3
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityMention
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext,
    CallbackQueryHandler,
)

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
SESSION_NAME = 'telegram_forwarder'

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise RuntimeError("CRITICAL ERROR: API_ID, API_HASH, and BOT_TOKEN must be set in your .env file.")

# --- DATABASE SETUP ---
DB_FILE = 'tasks.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            destination_id INTEGER NOT NULL,
            blacklist_words TEXT,
            whitelist_words TEXT,
            block_photos BOOLEAN NOT NULL DEFAULT 0,
            block_videos BOOLEAN NOT NULL DEFAULT 0,
            block_documents BOOLEAN NOT NULL DEFAULT 0,
            block_text BOOLEAN NOT NULL DEFAULT 0
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
    cursor.execute("SELECT * FROM tasks WHERE source_id = ?", (chat_id,))
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        return

    for task in tasks:
        _, _, destination_id, blacklist, whitelist, block_photos, block_videos, block_documents, block_text = task

        if (block_photos and message.photo) or \
           (block_videos and message.video) or \
           (block_documents and message.document and not message.video and not message.photo) or \
           (block_text and message.text and not message.media):
            print(f"Skipping message {message.id}: Media type is blocked by task settings.")
            continue

        full_text = (message.text or "").lower()
        mentions = []
        if message.entities:
            for entity in message.entities:
                if isinstance(entity, MessageEntityMention):
                    mention_text = message.text[entity.offset : entity.offset + entity.length].lower()
                    mentions.append(mention_text)

        if whitelist:
            whitelist_words = [word.strip().lower() for word in whitelist.split(',')]
            if not any(word in full_text for word in whitelist_words) and not any(mention in mentions for mention in whitelist_words):
                print(f"Skipping message {message.id}: No whitelist criteria met.")
                continue

        if blacklist:
            blacklist_words = [word.strip().lower() for word in blacklist.split(',')]
            if any(word in full_text for word in blacklist_words) or any(mention in mentions for mention in blacklist_words):
                print(f"Skipping message {message.id}: Blacklist criteria met.")
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
                )
            elif message.text:
                await client.send_message(
                    entity=destination_id,
                    message=message.text,
                )
            print("Message forwarded successfully.")
        except Exception as e:
            print(f"Could not forward message {message.id}. Error: {e}")
        finally:
            if downloaded_file_path and os.path.exists(downloaded_file_path):
                os.remove(downloaded_file_path)

# --- TELEGRAM BOT (THE INTERFACE) ---

SOURCE, DESTINATION, BLACKLIST, WHITELIST, MEDIA_FILTER, CONFIRMATION = range(6)

def build_media_filter_menu(context: CallbackContext):
    ud = context.user_data['media_filters']
    keyboard = [
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['photos'] else '🚫 BLOCK'} Photos", callback_data='toggle_photos')],
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['videos'] else '🚫 BLOCK'} Videos", callback_data='toggle_videos')],
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['documents'] else '🚫 BLOCK'} Files", callback_data='toggle_documents')],
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['text'] else '🚫 BLOCK'} Text", callback_data='toggle_text')],
        [InlineKeyboardButton("➡️ Done With Filters ➡️", callback_data='done_media_filter')]
    ]
    return InlineKeyboardMarkup(keyboard)

def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Welcome! Use /newtask to set up forwarding, /tasks to view, and /delete to remove.")

def new_task_start(update: Update, context: CallbackContext) -> int:
    context.user_data['media_filters'] = {'photos': False, 'videos': False, 'documents': False, 'text': False}
    update.message.reply_text("Let's set up a new task. First, define the Source chat.")
    return SOURCE

def get_chat_id(update, context):
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
        except: return None
    return True

def get_source(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context): return SOURCE
    context.user_data['source_id'] = context.user_data['chat_id']
    context.user_data['source_title'] = context.user_data['chat_title']
    update.message.reply_text(f"✅ Source set.\nNow, send the **Destination**.", parse_mode='Markdown')
    return DESTINATION

def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context): return DESTINATION
    context.user_data['destination_id'] = context.user_data['chat_id']
    context.user_data['destination_title'] = context.user_data['chat_title']
    update.message.reply_text("✅ Destination set.\nNow, send **Blacklist** words separated by a comma.\nSend /skip to ignore.", reply_markup=ReplyKeyboardRemove())
    return BLACKLIST

def get_blacklist(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() == '/skip': context.user_data['blacklist'] = None
    else: context.user_data['blacklist'] = update.message.text
    update.message.reply_text("✅ Blacklist set.\nNow, send **Whitelist** words.\nSend /skip to ignore.")
    return WHITELIST

def get_whitelist(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() == '/skip': context.user_data['whitelist'] = None
    else: context.user_data['whitelist'] = update.message.text
    reply_markup = build_media_filter_menu(context)
    update.message.reply_text("✅ Whitelist set.\nNow, configure which media types to **block**.", reply_markup=reply_markup)
    return MEDIA_FILTER

def media_filter_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    toggle = query.data.replace('toggle_', '')
    
    if toggle == 'done_media_filter':
        ud = context.user_data
        mf = ud['media_filters']
        summary = (
            f"Please confirm your new task:\n\n"
            f"➡️ **From:** {ud['source_title']}\n"
            f"↘️ **To:** {ud['destination_title']}\n\n"
            f"🚫 **Blacklist:** `{ud['blacklist'] or 'Not set'}`\n"
            f"✅ **Whitelist:** `{ud['whitelist'] or 'Not set'}`\n\n"
            f"**Media Forwarding Status:**\n"
            f"  Photos: {'✅' if not mf['photos'] else '🚫'}\n"
            f"  Videos: {'✅' if not mf['videos'] else '🚫'}\n"
            f"  Files: {'✅' if not mf['documents'] else '🚫'}\n"
            f"  Text: {'✅' if not mf['text'] else '🚫'}\n"
        )
        query.edit_message_text(summary, parse_mode='Markdown')
        update.effective_chat.send_message("Is this correct?", reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True))
        return CONFIRMATION

    context.user_data['media_filters'][toggle] = not context.user_data['media_filters'][toggle]
    reply_markup = build_media_filter_menu(context)
    query.edit_message_reply_markup(reply_markup)
    return MEDIA_FILTER

def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("Task cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    ud = context.user_data
    mf = ud['media_filters']
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (source_id, destination_id, blacklist_words, whitelist_words, block_photos, block_videos, block_documents, block_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ud['source_id'], ud['destination_id'], ud['blacklist'], ud['whitelist'], mf['photos'], mf['videos'], mf['documents'], mf['text'])
    )
    conn.commit()
    conn.close()
    
    update.message.reply_text("✅ Task saved successfully!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def list_tasks(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks")
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        update.message.reply_text("You have no active tasks.")
        return

    message_text = "Your active tasks:\n\n"
    for task in tasks:
        task_id, source, dest, blacklist, whitelist, no_photo, no_video, no_doc, no_text = task
        blocked = []
        if no_photo: blocked.append('Photos')
        if no_video: blocked.append('Videos')
        if no_doc: blocked.append('Files')
        if no_text: blocked.append('Text')
        
        message_text += (
            f"🔹 **Task ID:** {task_id}\n"
            f"   **From:** `{source}` -> **To:** `{dest}`\n"
            f"   **Blacklist:** `{blacklist or 'None'}`\n"
            f"   **Whitelist:** `{whitelist or 'None'}`\n"
            f"   **Blocking:** `{', '.join(blocked) or 'None'}`\n\n"
        )
    update.message.reply_text(message_text, parse_mode='Markdown')

def delete_task_start(update: Update, context: CallbackContext) -> int:
    list_tasks(update, context)
    update.message.reply_text("Please send the Task ID you want to delete.")
    return 0

def delete_task_confirm(update: Update, context: CallbackContext) -> int:
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
    update.message.reply_text('Operation cancelled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    init_db()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)],
            DESTINATION: [MessageHandler(Filters.all & ~Filters.command, get_destination)],
            # **THE FIX IS HERE:** We now use Filters.text to allow the /skip command
            BLACKLIST: [MessageHandler(Filters.text, get_blacklist)],
            WHITELIST: [MessageHandler(Filters.text, get_whitelist)],
            MEDIA_FILTER: [CallbackQueryHandler(media_filter_callback)],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    delete_handler = ConversationHandler(
        entry_points=[CommandHandler('delete', delete_task_start)],
        states={0: [MessageHandler(Filters.text & ~Filters.command, delete_task_confirm)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("tasks", list_tasks))
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