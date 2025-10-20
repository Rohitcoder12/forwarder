import sqlite3, asyncio, os, re, random, cv2
from PIL import Image
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters, ConversationHandler, CallbackContext, CallbackQueryHandler)

# --- CONFIGURATION & STATE ---
load_dotenv(); API_ID, API_HASH, BOT_TOKEN = os.getenv('API_ID'), os.getenv('API_HASH'), os.getenv('BOT_TOKEN')
SESSION_NAME, DB_FILE, MY_ID = 'telegram_forwarder', 'tasks.db', None
if not all([API_ID, API_HASH, BOT_TOKEN]): raise RuntimeError("API credentials must be set in .env file.")

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, source_id INTEGER, destination_ids TEXT,
        blacklist_words TEXT, whitelist_words TEXT, block_photos BOOLEAN, block_videos BOOLEAN, block_documents BOOLEAN,
        block_text BOOLEAN, block_replies_to_me BOOLEAN, block_my_messages BOOLEAN, beautiful_captions BOOLEAN,
        footer_text TEXT)
    ''')
    conn.commit(); conn.close()

# --- HELPER FUNCTIONS ---
async def generate_thumbnail(video_path):
    try:
        thumb_path = os.path.splitext(video_path)[0] + ".jpg"; cap = cv2.VideoCapture(video_path)
        if not cap.isOpened(): return None
        ret, frame = cap.read();
        if not ret: cap.release(); return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB); img = Image.fromarray(frame_rgb)
        img.thumbnail((320, 320)); img.save(thumb_path, "JPEG"); cap.release(); return thumb_path
    except Exception as e: print(f"Thumbnail generation failed: {e}"); return None

def create_beautiful_caption(original_text):
    link_pattern = r'https?://(?:tera[a-z]+|tinyurl)\.com/\S+'
    links = re.findall(link_pattern, original_text or "")
    if not links: return None
    
    emojis = random.sample(['😍', '🔥', '❤️', '😈', '💯', '💦', '🔞'], 2)
    
    # Start with the title, followed by a blank line
    caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}"]
    
    # **THE FIX IS HERE:** Add a newline after the V-number
    # This puts the link on the line below its label.
    video_links = [f"V{i}:\n{link}" for i, link in enumerate(links, 1)]
    caption_parts.extend(video_links)
    
    # Join everything with TWO newlines for clean separation
    return "\n\n".join(caption_parts)

# --- TELETHON CLIENT ENGINE ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID: return
    message = event.message; conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE source_id = ?", (event.chat_id,)); tasks = cursor.fetchall(); conn.close()
    if not tasks: return
    for task in tasks:
        _, _, dest_ids_str, bl, wl, no_p, no_v, no_d, no_t, no_r, no_m, beautify, footer = task
        if (no_m and message.sender_id == MY_ID) or (no_p and message.photo) or (no_v and message.video) or \
           (no_d and message.document and not message.video) or (no_t and message.text and not message.media): continue
        if no_r and message.is_reply:
            try:
                r_msg = await message.get_reply_message()
                if r_msg and r_msg.sender_id == MY_ID: continue
            except: pass
        full_text = (message.text or "").lower()
        if wl and not any(w.strip() in full_text for w in wl.lower().split(',')): continue
        if bl and any(w.strip() in full_text for w in bl.lower().split(',')): continue
        final_caption = message.text
        if beautify:
            new_caption = create_beautiful_caption(message.text)
            if new_caption: final_caption = new_caption
        if footer: final_caption = f"{final_caption or ''}\n\n{footer}"
        for dest_id_str in dest_ids_str.split(','):
            try: dest_id = int(dest_id_str.strip())
            except ValueError: continue
            print(f"Forwarding message {message.id} to {dest_id}")
            dl_path, thumb_path = None, None
            try:
                if message.media:
                    dl_path = await message.download_media();
                    if message.video: thumb_path = await generate_thumbnail(dl_path)
                    await client.send_file(dest_id, dl_path, caption=final_caption, thumb=thumb_path)
                elif message.text: await client.send_message(dest_id, final_caption)
            except Exception as e: print(f"Failed to send to {dest_id}: {e}")
            finally:
                if dl_path and os.path.exists(dl_path): os.remove(dl_path)
                if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

# --- TELEGRAM BOT INTERFACE ---
(SOURCE, DESTINATION, BLACKLIST, WHITELIST, MEDIA_FILTER, USER_FILTER, 
 CAPTION_SETTING, FOOTER_SETTING, CONFIRMATION, DELETE_TASK) = range(10)

def start(update: Update, context: CallbackContext): update.message.reply_text("Bot is running. Use /help to see available commands.")
def cancel(update: Update, context: CallbackContext): update.message.reply_text("Operation cancelled."); return ConversationHandler.END

def help_command(update: Update, context: CallbackContext):
    help_text = """
    **Welcome to your Advanced Auto-Forwarder Bot!**

    Here are the available commands and features:

    **AUTO FORWARDER**
    `/newtask` - Start a guided setup to create a new forwarding rule.
    `/tasks` - View all your active forwarding rules.
    `/delete` - Start the process to delete a forwarding rule.

    **Auto Forwarder Features:**
    - **Multiple Destinations:** Forward from one source to many channels at once.
    - **Content Filters:** Use **Whitelists** and **Blacklists**.
    - **Media Filters:** Individually block Photos, Videos, Files, or Text-only messages.
    - **User Filters:** Block your own messages or replies sent to you.
    - **Beautiful Captions:** Automatically reformat posts containing Tera-links.
    - **Custom Footer:** Add a standard footer text to all messages for a specific task.

    **GENERAL**
    `/cancel` - Cancels any ongoing setup process (`/newtask` or `/delete`).
    `/help` - Shows this help message.
    """
    update.message.reply_text(help_text, parse_mode='Markdown')

def new_task_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear(); context.user_data.update({'media_filters': {'photos': False, 'videos': False, 'documents': False, 'text': False}, 'user_filters': {'replies': False, 'own': False}, 'beautiful_captions': False, 'footer': None})
    update.message.reply_text("Let's configure a forwarder. First, define the Source chat."); return SOURCE

def get_chat_id(update, context, key_prefix):
    if update.message.forward_from_chat:
        id_val, title_val = update.message.forward_from_chat.id, update.message.forward_from_chat.title or "N/A"
        context.user_data[f'{key_prefix}_ids'], context.user_data[f'{key_prefix}_title'] = str(id_val), title_val
    else:
        try:
            ids = [int(i.strip()) for i in update.message.text.split(',')]
            context.user_data[f'{key_prefix}_ids'], context.user_data[f'{key_prefix}_title'] = update.message.text, f"{len(ids)} chat(s)" if len(ids) > 1 else f"ID: {ids[0]}"
        except: return None
    return True
def get_source(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'source'): return SOURCE
    update.message.reply_text("✅ Source set. Now, send Destination ID(s), separated by a comma."); return DESTINATION
def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'destination'): return DESTINATION
    update.message.reply_text("✅ Destination(s) set. Now, send Blacklist words.\nSend /skip to ignore."); return BLACKLIST
def get_blacklist(update: Update, context: CallbackContext) -> int:
    context.user_data['blacklist'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Blacklist set. Now, send Whitelist words.\nSend /skip to ignore."); return WHITELIST
def get_whitelist(update: Update, context: CallbackContext) -> int:
    context.user_data['whitelist'] = None if update.message.text.lower() == '/skip' else update.message.text
    ud = context.user_data['media_filters']; keyboard = [[InlineKeyboardButton(f"{'🚫' if ud[k] else '✅'} {k.capitalize()}", callback_data=f'media_{k}') for k in ud], [InlineKeyboardButton("➡️ Done", callback_data='media_done')]]
    update.message.reply_text("✅ Whitelist set. Configure media to block (🚫=BLOCK).", reply_markup=InlineKeyboardMarkup(keyboard)); return MEDIA_FILTER
def build_user_filter_menu(context: CallbackContext):
    ud = context.user_data['user_filters']
    keyboard = [[InlineKeyboardButton(f"{'🚫' if ud['replies'] else '✅'} Replies to Me", callback_data='user_replies'), InlineKeyboardButton(f"{'🚫' if ud['own'] else '✅'} My Msgs", callback_data='user_own')], [InlineKeyboardButton("➡️ Done", callback_data='user_done')]]
    return InlineKeyboardMarkup(keyboard)
def media_filter_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'done':
        query.edit_message_text("✅ Media filters saved."); query.message.reply_text("Next, configure user filters.", reply_markup=build_user_filter_menu(context)); return USER_FILTER
    context.user_data['media_filters'][action] = not context.user_data['media_filters'][action]
    ud = context.user_data['media_filters']; keyboard = [[InlineKeyboardButton(f"{'🚫' if ud[k] else '✅'} {k.capitalize()}", callback_data=f'media_{k}') for k in ud], [InlineKeyboardButton("➡️ Done", callback_data='media_done')]]
    query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard)); return MEDIA_FILTER
def user_filter_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'done':
        query.edit_message_text("✅ User filters saved.")
        keyboard = [[InlineKeyboardButton("Yes, Enable It", callback_data='caption_yes'), InlineKeyboardButton("No, Keep Original", callback_data='caption_no')]]
        query.message.reply_text("Enable 'Beautiful Captioning' for specific links?", reply_markup=InlineKeyboardMarkup(keyboard)); return CAPTION_SETTING
    context.user_data['user_filters']['replies' if action == 'replies' else 'own'] = not context.user_data['user_filters']['replies' if action == 'replies' else 'own']
    query.edit_message_reply_markup(build_user_filter_menu(context)); return USER_FILTER
def caption_setting_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query; query.answer(); context.user_data['beautiful_captions'] = (query.data == 'caption_yes')
    query.edit_message_text("✅ Caption settings saved.")
    query.message.reply_text("Now, send the **Footer Text** to add at the end of each message.\nSend /skip for no footer.")
    return FOOTER_SETTING
def get_footer(update: Update, context: CallbackContext) -> int:
    context.user_data['footer'] = None if update.message.text.lower() == '/skip' else update.message.text
    ud, mf, uf, bc, ft = context.user_data, context.user_data['media_filters'], context.user_data['user_filters'], context.user_data['beautiful_captions'], context.user_data['footer']
    summary = (f"Confirm Task:\n\n➡️ From: {ud['source_title']}\n↘️ To: {ud['destination_title']}\n\nBlacklist: `{ud['blacklist'] or 'N/A'}`\nWhitelist: `{ud['whitelist'] or 'N/A'}`\n"
               f"🚫 Blocking: {', '.join([k.capitalize() for k,v in mf.items() if v] + [n for n,v in [('Replies',uf['replies']),('My Msgs',uf['own'])] if v]) or 'None'}\n"
               f"✨ Beautiful Captions: {'✅' if bc else '🚫'}\n📝 Footer: {'✅' if ft else '🚫'}")
    update.message.reply_text(summary, parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True)); return CONFIRMATION
def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm': return cancel(update, context)
    ud, mf, uf, bc, ft = context.user_data, context.user_data['media_filters'], context.user_data['user_filters'], context.user_data['beautiful_captions'], context.user_data['footer']
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (source_id, destination_ids, blacklist_words, whitelist_words, block_photos, block_videos, block_documents, block_text, block_replies_to_me, block_my_messages, beautiful_captions, footer_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (int(ud['source_ids']), ud['destination_ids'], ud['blacklist'], ud['whitelist'], mf['photos'], mf['videos'], mf['documents'], mf['text'], uf['replies'], uf['own'], bc, ft))
    conn.commit(); conn.close()
    update.message.reply_text("✅ Task saved successfully!", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END
def list_tasks(update: Update, context: CallbackContext):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT id, source_id, destination_ids, beautiful_captions FROM tasks"); tasks = cursor.fetchall(); conn.close()
    if not tasks: update.message.reply_text("No active tasks."); return
    msg = "".join([f"🔹 ID: {t[0]}\n   From: `{t[1]}`\n   To: `{t[2]}`\n   Captions: {'✅' if t[3] else '🚫'}\n\n" for t in tasks])
    update.message.reply_text("Your active tasks:\n\n" + msg, parse_mode='Markdown')
def delete_task_start(update: Update, context: CallbackContext) -> int:
    list_tasks(update, context); update.message.reply_text("Please send the Task ID to delete."); return DELETE_TASK
def delete_task_confirm(update: Update, context: CallbackContext) -> int:
    try:
        task_id = int(update.message.text); conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,)); conn.commit()
        if cursor.rowcount > 0: update.message.reply_text(f"✅ Task {task_id} deleted.")
        else: update.message.reply_text(f"❌ Task {task_id} not found.")
        conn.close()
    except ValueError: update.message.reply_text("Invalid ID.")
    return ConversationHandler.END

# --- MAIN EXECUTION BLOCK ---
async def main():
    global MY_ID; init_db(); updater = Updater(BOT_TOKEN); dp = updater.dispatcher
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_start)],
        states={
            SOURCE: [MessageHandler(Filters.all & ~Filters.command, get_source)], DESTINATION: [MessageHandler(Filters.text & ~Filters.command, get_destination)],
            BLACKLIST: [MessageHandler(Filters.text, get_blacklist)], WHITELIST: [MessageHandler(Filters.text, get_whitelist)],
            MEDIA_FILTER: [CallbackQueryHandler(media_filter_callback, pattern='^media_')], USER_FILTER: [CallbackQueryHandler(user_filter_callback, pattern='^user_')],
            CAPTION_SETTING: [CallbackQueryHandler(caption_setting_callback, pattern='^caption_')], FOOTER_SETTING: [MessageHandler(Filters.text, get_footer)],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    delete_handler = ConversationHandler(entry_points=[CommandHandler('delete', delete_task_start)], states={DELETE_TASK: [MessageHandler(Filters.text & ~Filters.command, delete_task_confirm)]}, fallbacks=[CommandHandler('cancel', cancel)])
    
    dp.add_handler(conv_handler); dp.add_handler(delete_handler)
    dp.add_handler(CommandHandler("start", start)); dp.add_handler(CommandHandler("tasks", list_tasks))
    dp.add_handler(CommandHandler("help", help_command))
    
    updater.start_polling(); print("Control Bot started...")
    await client.start()
    me = await client.get_me(); MY_ID = me.id
    print(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): print("Bot stopped gracefully.")