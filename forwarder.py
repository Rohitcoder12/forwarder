import sqlite3
import asyncio
import os
import re
import random
import cv2
from PIL import Image
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
MY_ID = None

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise RuntimeError("CRITICAL ERROR: API_ID, API_HASH, and BOT_TOKEN must be set in your .env file.")

# --- DATABASE SETUP ---
DB_FILE = 'tasks.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            destination_ids TEXT NOT NULL, blacklist_words TEXT, whitelist_words TEXT,
            block_photos BOOLEAN NOT NULL DEFAULT 0, block_videos BOOLEAN NOT NULL DEFAULT 0,
            block_documents BOOLEAN NOT NULL DEFAULT 0, block_text BOOLEAN NOT NULL DEFAULT 0,
            block_replies_to_me BOOLEAN NOT NULL DEFAULT 0, block_my_messages BOOLEAN NOT NULL DEFAULT 0,
            beautiful_captions BOOLEAN NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# --- HELPER FUNCTION FOR THUMBNAILS ---
async def generate_thumbnail(video_path):
    try:
        thumb_path = os.path.splitext(video_path)[0] + ".jpg"
        cap = cv2.VideoCapture(video_path)
        ret, frame = cap.read()
        if not ret:
            cap.release()
            return None
        
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        img.thumbnail((320, 320)) 
        img.save(thumb_path, "JPEG")
        cap.release()
        return thumb_path
    except Exception as e:
        print(f"Thumbnail generation failed: {e}")
        return None

# --- TELETHON CLIENT (THE ENGINE) ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    chat_id = event.chat_id
    message = event.message
    
    if not MY_ID: return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE source_id = ?", (chat_id,))
    tasks = cursor.fetchall()
    conn.close()

    if not tasks: return

    for task in tasks:
        _, _, dest_ids_str, bl, wl, no_p, no_v, no_d, no_t, no_r, no_m, beautify = task

        if (no_m and message.sender_id == MY_ID) or \
           (no_p and message.photo) or (no_v and message.video) or \
           (no_d and message.document and not message.video) or \
           (no_t and message.text and not message.media):
            continue
        if no_r and message.is_reply:
            try:
                r_msg = await message.get_reply_message()
                if r_msg and r_msg.sender_id == MY_ID: continue
            except: pass

        full_text = (message.text or "").lower()
        if wl and not any(w in full_text for w in wl.lower().split(',')): continue
        if bl and any(w in full_text for w in bl.lower().split(',')): continue

        final_caption = message.text
        if beautify:
            links = re.findall(r'https?://(?:teraboxlink|terafileshare|tinyurl)\.com/\S+', message.text or "")
            if links:
                emojis = random.sample(['😍', '🔥', '❤️', '😈', '💯', '💦', '🔞'], 2)
                caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}\n"]
                caption_parts.extend([f"V{i}: {link}" for i, link in enumerate(links, 1)])
                final_caption = "\n".join(caption_parts)

        for dest_id_str in dest_ids_str.split(','):
            try: dest_id = int(dest_id_str.strip())
            except ValueError: continue

            print(f"Forwarding message {message.id} to {dest_id}")
            dl_path, thumb_path = None, None
            try:
                if message.media:
                    dl_path = await message.download_media()
                    if message.video: thumb_path = await generate_thumbnail(dl_path)
                    await client.send_file(dest_id, dl_path, caption=final_caption, thumb=thumb_path)
                elif message.text:
                    await client.send_message(dest_id, final_caption)
            except Exception as e: print(f"Failed to send to {dest_id}: {e}")
            finally:
                if dl_path and os.path.exists(dl_path): os.remove(dl_path)
                if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

# --- TELEGRAM BOT (THE INTERFACE) ---

SOURCE, DESTINATION, BLACKLIST, WHITELIST, MEDIA_FILTER, USER_FILTER, CAPTION_SETTING, CONFIRMATION = range(8)

def build_menu(buttons, n_cols): return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]

def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Welcome! Use /newtask, /tasks, and /delete.")

def new_task_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    context.user_data.update({
        'media_filters': {'photos': False, 'videos': False, 'documents': False, 'text': False},
        'user_filters': {'replies': False, 'own': False},
        'beautiful_captions': False
    })
    update.message.reply_text("Let's start. First, define the Source chat.")
    return SOURCE

def get_chat_id(update, context, key_prefix):
    if update.message.forward_from_chat:
        id_val = update.message.forward_from_chat.id
        title_val = update.message.forward_from_chat.title or "N/A"
        context.user_data[f'{key_prefix}_ids'] = str(id_val)
        context.user_data[f'{key_prefix}_title'] = title_val
    else:
        try:
            ids = [int(i.strip()) for i in update.message.text.split(',')]
            context.user_data[f'{key_prefix}_ids'] = update.message.text
            context.user_data[f'{key_prefix}_title'] = f"{len(ids)} chat(s)" if len(ids) > 1 else f"ID: {ids[0]}"
        except: return None
    return True

def get_source(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'source'): return SOURCE
    update.message.reply_text("✅ Source set. Now, send the Destination(s).\nTo send to multiple, separate IDs with a comma.", parse_mode='Markdown')
    return DESTINATION

def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'destination'): return DESTINATION
    update.message.reply_text("✅ Destination(s) set. Now, send Blacklist words.\nSend /skip to ignore.", reply_markup=ReplyKeyboardRemove())
    return BLACKLIST

def get_blacklist(update: Update, context: CallbackContext) -> int:
    context.user_data['blacklist'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Blacklist set. Now, send Whitelist words.\nSend /skip to ignore.")
    return WHITELIST

def get_whitelist(update: Update, context: CallbackContext) -> int:
    context.user_data['whitelist'] = None if update.message.text.lower() == '/skip' else update.message.text
    ud = context.user_data['media_filters']
    keyboard = [[InlineKeyboardButton(f"{'🚫' if ud[k] else '✅'} {k.capitalize()}", callback_data=f'media_{k}') for k in ud], [InlineKeyboardButton("➡️ Done", callback_data='media_done')]]
    update.message.reply_text("✅ Whitelist set. Configure media to block (🚫=BLOCK).", reply_markup=InlineKeyboardMarkup(keyboard))
    return MEDIA_FILTER

def filter_callback(update: Update, context: CallbackContext, filter_type, next_state, build_next_menu):
    query = update.callback_query
    query.answer()
    action = query.data.split('_')[1]

    if action == 'done':
        query.edit_message_text(f"✅ {filter_type.capitalize()} filters saved.")
        reply_markup = build_next_menu(context)
        query.message.reply_text(f"Next, configure {build_next_menu.__name__.split('_')[1]} filters.", reply_markup=reply_markup)
        return next_state

    context.user_data[f'{filter_type}_filters'][action] = not context.user_data[f'{filter_type}_filters'][action]
    query.edit_message_reply_markup(globals()[f'build_{filter_type}_filter_menu'](context))
    return globals()[f'{filter_type.upper()}_FILTER']

def media_filter_callback(update: Update, context: CallbackContext):
    return filter_callback(update, context, 'media', USER_FILTER, build_user_filter_menu)

def user_filter_callback(update: Update, context: CallbackContext):
    return filter_callback(update, context, 'user', CAPTION_SETTING, lambda ctx: InlineKeyboardMarkup([[InlineKeyboardButton("Yes, Enable It", callback_data='caption_yes'), InlineKeyboardButton("No, Keep Original", callback_data='caption_no')]]))

def caption_setting_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    context.user_data['beautiful_captions'] = (query.data == 'caption_yes')
    ud, mf, uf, bc = context.user_data, context.user_data['media_filters'], context.user_data['user_filters'], context.user_data['beautiful_captions']
    summary = (f"Confirm Task:\n\n"
               f"➡️ From: {ud['source_title']} ({ud['source_ids']})\n"
               f"↘️ To: {ud['destination_title']} ({ud['destination_ids']})\n\n"
               f"Blacklist: `{ud['blacklist'] or 'N/A'}`\nWhitelist: `{ud['whitelist'] or 'N/A'}`\n\n"
               f"🚫 Blocking: {', '.join([k for k, v in mf.items() if v] + [k for k, v in {'Replies':uf['replies'], 'My Msgs':uf['own']}.items() if v]) or 'None'}\n"
               f"✨ Beautiful Captions: {'✅ Yes' if bc else '🚫 No'}")
    query.edit_message_text(summary, parse_mode='Markdown')
    update.effective_chat.send_message("Is this correct?", reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True))
    return CONFIRMATION

def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("Task cancelled.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END
    ud, mf, uf, bc = context.user_data, context.user_data['media_filters'], context.user_data['user_filters'], context.user_data['beautiful_captions']
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (source_id, destination_ids, blacklist_words, whitelist_words, block_photos, block_videos, block_documents, block_text, block_replies_to_me, block_my_messages, beautiful_captions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (int(ud['source_ids']), ud['destination_ids'], ud['blacklist'], ud['whitelist'], mf['photos'], mf['videos'], mf['documents'], mf['text'], uf['replies'], uf['own'], bc))
    conn.commit()
    conn.close()
    update.message.reply_text("✅ Task saved successfully!", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

def list_tasks(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT id, source_id, destination_ids, beautiful_captions FROM tasks")
    tasks = cursor.fetchall()
    conn.close()
    if not tasks: update.message.reply_text("No active tasks."); return
    msg = "".join([f"🔹 ID: {t[0]}\n   From: `{t[1]}`\n   To: `{t[2]}`\n   Captions: {'✅' if t[3] else '🚫'}\n\n" for t in tasks])
    update.message.reply_text("Your active tasks:\n\n" + msg, parse_mode='Markdown')

# ... (delete_task_start, delete_task_confirm, cancel are unchanged) ...

async def main():
    global MY_ID
    init_db()
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)],
            DESTINATION: [MessageHandler(Filters.text & ~Filters.command, get_destination)],
            BLACKLIST: [MessageHandler(Filters.text, get_blacklist)],
            WHITELIST: [MessageHandler(Filters.text, get_whitelist)],
            MEDIA_FILTER: [CallbackQueryHandler(media_filter_callback, pattern='^media_')],
            USER_FILTER: [CallbackQueryHandler(user_filter_callback, pattern='^user_')],
            CAPTION_SETTING: [CallbackQueryHandler(caption_setting_callback, pattern='^caption_')],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    # ... (add handlers, start bot, etc. as before) ...
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("tasks", list_tasks))
    # Add delete handler
    
    updater.start_polling()
    print("Control Bot started...")
    await client.start()
    me = await client.get_me()
    MY_ID = me.id
    print(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped gracefully.")