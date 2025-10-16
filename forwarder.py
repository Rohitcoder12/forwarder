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
MY_ID = None # Will be populated with the user's ID at startup

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
            block_text BOOLEAN NOT NULL DEFAULT 0,
            block_replies_to_me BOOLEAN NOT NULL DEFAULT 0,
            block_my_messages BOOLEAN NOT NULL DEFAULT 0
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
    
    if not MY_ID: # Safety check
        print("Warning: MY_ID not set. User filters will not work.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE source_id = ?", (chat_id,))
    tasks = cursor.fetchall()
    conn.close()

    if not tasks: return

    for task in tasks:
        # Unpack all task settings from the database row
        _, _, dest_id, bl, wl, no_photo, no_video, no_doc, no_text, no_reply_to_me, no_my_msg = task

        # --- NEW: User-Based Filters ---
        if no_my_msg and message.sender_id == MY_ID:
            print(f"Skipping message {message.id}: It's one of my own messages.")
            continue
        
        if no_reply_to_me and message.is_reply:
            try:
                replied_to_msg = await message.get_reply_message()
                if replied_to_msg and replied_to_msg.sender_id == MY_ID:
                    print(f"Skipping message {message.id}: It's a reply to my own message.")
                    continue
            except Exception as e:
                print(f"Could not check reply for message {message.id}: {e}")

        # Media Type Filtering
        if (no_photo and message.photo) or \
           (no_video and message.video) or \
           (no_doc and message.document and not message.video and not message.photo) or \
           (no_text and message.text and not message.media):
            print(f"Skipping message {message.id}: Media type is blocked.")
            continue

        # Whitelist/Blacklist Logic
        full_text = (message.text or "").lower()
        mentions = [message.text[entity.offset:entity.offset + entity.length].lower() for entity in (message.entities or []) if isinstance(entity, MessageEntityMention)]

        if wl and not any(word in full_text for word in wl.lower().split(',')) and not any(mention in mentions for mention in wl.lower().split(',')):
            print(f"Skipping message {message.id}: No whitelist criteria met.")
            continue

        if bl and (any(word in full_text for word in bl.lower().split(',')) or any(mention in mentions for mention in bl.lower().split(','))):
            print(f"Skipping message {message.id}: Blacklist criteria met.")
            continue
        
        print(f"Forwarding message {message.id} from {chat_id} to {dest_id}")
        
        downloaded_file_path = None
        try:
            if message.media:
                downloaded_file_path = await message.download_media()
                await client.send_file(entity=dest_id, file=downloaded_file_path, caption=message.text)
            elif message.text:
                await client.send_message(entity=dest_id, message=message.text)
            print("Message forwarded successfully.")
        except Exception as e:
            print(f"Could not forward message {message.id}. Error: {e}")
        finally:
            if downloaded_file_path and os.path.exists(downloaded_file_path):
                os.remove(downloaded_file_path)

# --- TELEGRAM BOT (THE INTERFACE) ---

SOURCE, DESTINATION, BLACKLIST, WHITELIST, MEDIA_FILTER, USER_FILTER, CONFIRMATION = range(7)

def build_media_filter_menu(context: CallbackContext):
    ud = context.user_data['media_filters']
    keyboard = [[InlineKeyboardButton(f"{'✅ FORWARD' if not v else '🚫 BLOCK'} {k.capitalize()}", callback_data=f'media_{k}')] for k, v in ud.items()]
    keyboard.append([InlineKeyboardButton("➡️ Done With Media Filters ➡️", callback_data='media_done')])
    return InlineKeyboardMarkup(keyboard)

def build_user_filter_menu(context: CallbackContext):
    ud = context.user_data['user_filters']
    keyboard = [
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['replies'] else '🚫 BLOCK'} Bot Replies to Me", callback_data='user_replies')],
        [InlineKeyboardButton(f"{'✅ FORWARD' if not ud['own'] else '🚫 BLOCK'} My Own Messages", callback_data='user_own')],
        [InlineKeyboardButton("➡️ Finish Setup ➡️", callback_data='user_done')]
    ]
    return InlineKeyboardMarkup(keyboard)

def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Welcome! Use /newtask, /tasks, and /delete.")

def new_task_start(update: Update, context: CallbackContext) -> int:
    context.user_data['media_filters'] = {'photos': False, 'videos': False, 'documents': False, 'text': False}
    context.user_data['user_filters'] = {'replies': False, 'own': False}
    update.message.reply_text("Let's start a new task. First, define the Source chat.")
    return SOURCE

def get_chat_id(update, context):
    # This helper is unchanged
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
    update.message.reply_text("✅ Source set. Now, send the **Destination**.", parse_mode='Markdown')
    return DESTINATION

def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context): return DESTINATION
    context.user_data['destination_id'] = context.user_data['chat_id']
    context.user_data['destination_title'] = context.user_data['chat_title']
    update.message.reply_text("✅ Destination set. Now, send **Blacklist** words (comma-separated).\nSend /skip to ignore.", reply_markup=ReplyKeyboardRemove())
    return BLACKLIST

def get_blacklist(update: Update, context: CallbackContext) -> int:
    context.user_data['blacklist'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Blacklist set. Now, send **Whitelist** words.\nSend /skip to ignore.")
    return WHITELIST

def get_whitelist(update: Update, context: CallbackContext) -> int:
    context.user_data['whitelist'] = None if update.message.text.lower() == '/skip' else update.message.text
    reply_markup = build_media_filter_menu(context)
    update.message.reply_text("✅ Whitelist set. Now, configure which media types to **block**.", reply_markup=reply_markup)
    return MEDIA_FILTER

def media_filter_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    action = query.data.split('_')[1]
    
    if action == 'done':
        query.edit_message_text("✅ Media filters saved.")
        reply_markup = build_user_filter_menu(context)
        query.message.reply_text("Next, configure user-specific message filters.", reply_markup=reply_markup)
        return USER_FILTER

    context.user_data['media_filters'][action] = not context.user_data['media_filters'][action]
    query.edit_message_reply_markup(build_media_filter_menu(context))
    return MEDIA_FILTER

def user_filter_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    action = query.data.split('_')[1]

    if action == 'done':
        ud, mf, uf = context.user_data, context.user_data['media_filters'], context.user_data['user_filters']
        summary = (
            f"Please confirm your final task settings:\n\n"
            f"➡️ **From:** {ud['source_title']}\n"
            f"↘️ **To:** {ud['destination_title']}\n\n"
            f"🚫 **Blacklist:** `{ud['blacklist'] or 'Not set'}`\n"
            f"✅ **Whitelist:** `{ud['whitelist'] or 'Not set'}`\n\n"
            f"**Media Blocking:**\n"
            f"  Photos: {'🚫' if mf['photos'] else '✅'} | Videos: {'🚫' if mf['videos'] else '✅'}\n"
            f"  Files: {'🚫' if mf['documents'] else '✅'} | Text: {'🚫' if mf['text'] else '✅'}\n\n"
            f"**User Blocking:**\n"
            f"  Replies to Me: {'🚫' if uf['replies'] else '✅'}\n"
            f"  My Messages: {'🚫' if uf['own'] else '✅'}"
        )
        query.edit_message_text(summary, parse_mode='Markdown')
        update.effective_chat.send_message("Is this correct?", reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True))
        return CONFIRMATION

    context.user_data['user_filters'][action] = not context.user_data['user_filters'][action]
    query.edit_message_reply_markup(build_user_filter_menu(context))
    return USER_FILTER

def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("Task cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    ud, mf, uf = context.user_data, context.user_data['media_filters'], context.user_data['user_filters']
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (source_id, destination_id, blacklist_words, whitelist_words, block_photos, block_videos, block_documents, block_text, block_replies_to_me, block_my_messages) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ud['source_id'], ud['destination_id'], ud['blacklist'], ud['whitelist'], mf['photos'], mf['videos'], mf['documents'], mf['text'], uf['replies'], uf['own'])
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

    if not tasks: update.message.reply_text("You have no active tasks."); return

    message_text = "Your active tasks:\n\n"
    for task in tasks:
        task_id, source, dest, bl, wl, no_photo, no_video, no_doc, no_text, no_reply, no_own = task
        media_blocked = [name for name, blocked in [('Photos', no_photo), ('Videos', no_video), ('Files', no_doc), ('Text', no_text)] if blocked]
        user_blocked = [name for name, blocked in [('Replies to Me', no_reply), ('My Own Msgs', no_own)] if blocked]
        
        message_text += (
            f"🔹 **Task ID:** {task_id}\n"
            f"   **From:** `{source}` -> **To:** `{dest}`\n"
            f"   **Blacklist:** `{bl or 'None'}`\n"
            f"   **Whitelist:** `{wl or 'None'}`\n"
            f"   **Blocking Media:** `{', '.join(media_blocked) or 'None'}`\n"
            f"   **Blocking User:** `{', '.join(user_blocked) or 'None'}`\n\n"
        )
    update.message.reply_text(message_text, parse_mode='Markdown')

def delete_task_start(update: Update, context: CallbackContext) -> int:
    # This is unchanged
    list_tasks(update, context)
    update.message.reply_text("Please send the Task ID you want to delete.")
    return 0

def delete_task_confirm(update: Update, context: CallbackContext) -> int:
    # This is unchanged
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
    # This is unchanged
    update.message.reply_text('Operation cancelled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    global MY_ID
    init_db()
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)],
            DESTINATION: [MessageHandler(Filters.all & ~Filters.command, get_destination)],
            BLACKLIST: [MessageHandler(Filters.text & ~Filters.command | Filters.command, get_blacklist)],
            WHITELIST: [MessageHandler(Filters.text & ~Filters.command | Filters.command, get_whitelist)],
            MEDIA_FILTER: [CallbackQueryHandler(media_filter_callback, pattern='^media_')],
            USER_FILTER: [CallbackQueryHandler(user_filter_callback, pattern='^user_')],
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
    me = await client.get_me()
    MY_ID = me.id
    print(f"Telethon client started as: {me.first_name} (@{me.username}, ID: {MY_ID})")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped gracefully.")