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
        footer_text TEXT, remove_texts TEXT, replace_rules TEXT)
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
    link_pattern = r'https?://(?:tera[a-z]+|tinyurl|teraboxurl)\.com/\S+'
    links = re.findall(link_pattern, original_text or "")
    if not links: return None
    emojis = random.sample(['😍', '🔥', '❤️', '😈', '💯', '💦', '🔞'], 2)
    caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}"]
    video_links = [f"V{i}:\n{link}" for i, link in enumerate(links, 1)]
    caption_parts.extend(video_links); return "\n\n".join(caption_parts)

# --- TELETHON CLIENT ENGINE ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID: return
    message = event.message; conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE source_id = ?", (event.chat_id,)); tasks = cursor.fetchall(); conn.close()
    if not tasks: return
    for task in tasks:
        _, _, dest_ids_str, bl, wl, no_p, no_v, no_d, no_t, no_r, no_m, beautify, footer, remove, replace = task
        if (no_m and message.sender_id == MY_ID) or (no_p and message.photo) or (no_v and message.video) or \
           (no_d and message.document and not message.video) or (no_t and message.text and not message.media): continue
        if no_r and message.is_reply:
            try:
                r_msg = await message.get_reply_message()
                if r_msg and r_msg.sender_id == MY_ID: continue
            except: pass
        
        final_caption = message.text
        
        # --- **THE FINAL, CORRECTED LOGIC** ---
        # 1. Apply Remove Rules FIRST, line by line comparison
        if remove and final_caption:
            lines_to_remove = {line.strip() for line in remove.splitlines() if line.strip()}
            original_lines = final_caption.splitlines()
            kept_lines = [line for line in original_lines if line.strip() not in lines_to_remove]
            final_caption = "\n".join(kept_lines)

        # 2. Apply Replace Rules SECOND
        if replace and final_caption:
            for rule in replace.splitlines():
                if '=>' in rule:
                    find, repl = rule.split('=>', 1)
                    final_caption = final_caption.replace(find.strip(), repl.strip())
        
        # 3. Apply Beautiful Captioning THIRD
        if beautify:
            new_caption = create_beautiful_caption(final_caption)
            if new_caption: final_caption = new_caption
        
        # 4. Clean up excess blank lines and Apply Footer LAST
        if final_caption:
            # Re-split and join to remove blank lines left from the removal process
            cleaned_lines = [line for line in final_caption.splitlines() if line.strip()]
            final_caption = "\n".join(cleaned_lines)
        
        if footer:
            final_caption = f"{final_caption}\n\n{footer}"
        
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
 CAPTION_SETTING, FOOTER_SETTING, REMOVE_SETTING, REPLACE_SETTING, CONFIRMATION, DELETE_TASK) = range(12)

# ... (The entire bot interface section is unchanged and correct) ...
def start(update: Update, context: CallbackContext): update.message.reply_text("Bot is running. Use /help to see commands.")
def cancel(update: Update, context: CallbackContext): update.message.reply_text("Operation cancelled."); return ConversationHandler.END
def help_command(update: Update, context: CallbackContext):
    help_text = """**Advanced Auto-Forwarder Bot Help**
    `/newtask` - Create a new forwarding rule.
    `/tasks` - View all active rules.
    `/delete` - Delete a rule.
    `/cancel` - Cancel any setup process.
    `/help` - Shows this message.
    
    **Features in `/newtask`:**
    - **Remove Text:** Provide text (multi-line supported) to be deleted from captions.
    - **Replace Text:** Provide rules like `find => replace` (one rule per line) to modify captions."""
    update.message.reply_text(help_text, parse_mode='Markdown')
def new_task_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear(); context.user_data.update({'media_filters': {}, 'user_filters': {}, 'beautiful_captions': False, 'footer': None, 'remove': None, 'replace': None})
    update.message.reply_text("Let's configure a forwarder. First, define the Source chat."); return SOURCE
def get_chat_id(update, context, key_prefix):
    if update.message.forward_from_chat:
        id_val, title_val = update.message.forward_from_chat.id, update.message.forward_from_chat.title or "N/A"
        context.user_data[f'{key_prefix}_ids'], context.user_data[f'{key_prefix}_title'] = str(id_val), title_val
    else:
        try:
            ids = [int(i.strip()) for i in update.message.text.split(',')]
            context.user_data[f'{key_prefix}_ids'], context.user_data[f'{key_prefix}_title'] = update.message.text, f"{len(ids)} chat(s)"
        except: return None
    return True
def get_source(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'source'): return SOURCE
    update.message.reply_text("✅ Source set. Now, send Destination ID(s)."); return DESTINATION
def get_destination(update: Update, context: CallbackContext) -> int:
    if not get_chat_id(update, context, 'destination'): return DESTINATION
    update.message.reply_text("✅ Destination(s) set. Now, send Blacklist words.\nSend /skip."); return BLACKLIST
def get_blacklist(update: Update, context: CallbackContext) -> int:
    context.user_data['blacklist'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Blacklist set. Now, send Whitelist words.\nSend /skip."); return WHITELIST
def get_whitelist(update: Update, context: CallbackContext) -> int:
    context.user_data['whitelist'] = None if update.message.text.lower() == '/skip' else update.message.text
    ud = context.user_data['media_filters']; keyboard = [[InlineKeyboardButton(f"{'🚫' if ud.get(k) else '✅'} {k.capitalize()}", callback_data=f'media_{k}') for k in ['photos','videos','documents','text']], [InlineKeyboardButton("➡️ Done", callback_data='media_done')]]
    update.message.reply_text("✅ Whitelist set. Configure media to block.", reply_markup=InlineKeyboardMarkup(keyboard)); return MEDIA_FILTER
def build_user_filter_menu(context: CallbackContext):
    ud = context.user_data['user_filters']
    keyboard = [[InlineKeyboardButton(f"{'🚫' if ud.get('replies') else '✅'} Replies to Me", callback_data='user_replies'), InlineKeyboardButton(f"{'🚫' if ud.get('own') else '✅'} My Msgs", callback_data='user_own')], [InlineKeyboardButton("➡️ Done", callback_data='user_done')]]
    return InlineKeyboardMarkup(keyboard)
def media_filter_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'done':
        query.edit_message_text("✅ Media filters saved."); query.message.reply_text("Next, configure user filters.", reply_markup=build_user_filter_menu(context)); return USER_FILTER
    context.user_data['media_filters'][action] = not context.user_data['media_filters'].get(action, False)
    ud = context.user_data['media_filters']; keyboard = [[InlineKeyboardButton(f"{'🚫' if ud.get(k) else '✅'} {k.capitalize()}", callback_data=f'media_{k}') for k in ['photos','videos','documents','text']], [InlineKeyboardButton("➡️ Done", callback_data='media_done')]]
    query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard)); return MEDIA_FILTER
def user_filter_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'done':
        query.edit_message_text("✅ User filters saved.")
        keyboard = [[InlineKeyboardButton("Yes", callback_data='caption_yes'), InlineKeyboardButton("No", callback_data='caption_no')]]
        query.message.reply_text("Enable 'Beautiful Captioning'?", reply_markup=InlineKeyboardMarkup(keyboard)); return CAPTION_SETTING
    context.user_data['user_filters']['replies' if action == 'replies' else 'own'] = not context.user_data['user_filters'].get('replies' if action == 'replies' else 'own', False)
    query.edit_message_reply_markup(build_user_filter_menu(context)); return USER_FILTER
def caption_setting_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query; query.answer(); context.user_data['beautiful_captions'] = (query.data == 'caption_yes')
    query.edit_message_text("✅ Caption settings saved.")
    query.message.reply_text("Now, send the Footer Text.\nSend /skip for no footer."); return FOOTER_SETTING
def get_footer(update: Update, context: CallbackContext) -> int:
    context.user_data['footer'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Footer set.\n\nNext, send text to **Remove**. Put each phrase on a new line.\nSend /skip to ignore."); return REMOVE_SETTING
def get_remove_texts(update: Update, context: CallbackContext) -> int:
    context.user_data['remove'] = None if update.message.text.lower() == '/skip' else update.message.text
    update.message.reply_text("✅ Removal rules set.\n\nFinally, send text to **Replace** in the format `find => replace`, one rule per line.\nSend /skip to ignore."); return REPLACE_SETTING
def get_replace_rules(update: Update, context: CallbackContext) -> int:
    context.user_data['replace'] = None if update.message.text.lower() == '/skip' else update.message.text
    ud = context.user_data
    summary = (f"Confirm Task:\n\n➡️ From: {ud['source_title']}\n↘️ To: {ud['destination_title']}\n"
               f"✨ Captions: {'✅' if ud.get('beautiful_captions') else '🚫'}\n📝 Footer: {'✅' if ud.get('footer') else '🚫'}\n"
               f"✂️ Remove: {'✅' if ud.get('remove') else '🚫'}\n🔄 Replace: {'✅' if ud.get('replace') else '🚫'}")
    update.message.reply_text(summary, parse_mode='Markdown', reply_markup=ReplyKeyboardMarkup([['Confirm', 'Cancel']], one_time_keyboard=True)); return CONFIRMATION
def save_task(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() != 'confirm': return cancel(update, context)
    ud, mf, uf = context.user_data, context.user_data.get('media_filters',{}), context.user_data.get('user_filters',{})
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (source_id, destination_ids, blacklist_words, whitelist_words, block_photos, block_videos, block_documents, block_text, block_replies_to_me, block_my_messages, beautiful_captions, footer_text, remove_texts, replace_rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (int(ud['source_ids']), ud['destination_ids'], ud.get('blacklist'), ud.get('whitelist'), mf.get('photos',0), mf.get('videos',0), mf.get('documents',0), mf.get('text',0), uf.get('replies',0), uf.get('own',0), ud.get('beautiful_captions',0), ud.get('footer'), ud.get('remove'), ud.get('replace')))
    conn.commit(); conn.close()
    update.message.reply_text("✅ Task saved!", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END
def list_tasks(update: Update, context: CallbackContext):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT id, source_id, destination_ids FROM tasks"); tasks = cursor.fetchall(); conn.close()
    if not tasks: update.message.reply_text("No active tasks."); return
    msg = "".join([f"🔹 ID: {t[0]}\n   From: `{t[1]}`\n   To: `{t[2]}`\n\n" for t in tasks])
    update.message.reply_text("Your active tasks:\n\n" + msg, parse_mode='Markdown')
def delete_task_start(update: Update, context: CallbackContext) -> int:
    list_tasks(update, context); update.message.reply_text("Send the Task ID to delete."); return DELETE_TASK
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
            REMOVE_SETTING: [MessageHandler(Filters.text, get_remove_texts)], REPLACE_SETTING: [MessageHandler(Filters.text, get_replace_rules)],
            CONFIRMATION: [MessageHandler(Filters.regex('^(Confirm|Cancel)$'), save_task)],
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    delete_handler = ConversationHandler(entry_points=[CommandHandler('delete', delete_task_start)], states={DELETE_TASK: [MessageHandler(Filters.text & ~Filters.command, delete_task_confirm)]}, fallbacks=[CommandHandler('cancel', cancel)])
    dp.add_handler(conv_handler); dp.add_handler(delete_handler)
    dp.add_handler(CommandHandler("start", start)); dp.add_handler(CommandHandler("tasks", list_tasks)); dp.add_handler(CommandHandler("help", help_command))
    updater.start_polling(); print("Control Bot started...")
    await client.start()
    me = await client.get_me(); MY_ID = me.id
    print(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): print("Bot stopped gracefully.")