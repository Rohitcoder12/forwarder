import asyncio
import os
import re
import random
import logging
from dotenv import load_dotenv
import cv2
from PIL import Image
from telethon import TelegramClient, events
from telethon.tl.types import Message, DocumentAttributeVideo
from telethon.errors import FloodWaitError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler
from pymongo import MongoClient
from datetime import datetime

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
LOGGER = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
SESSION_NAME = "telegram_forwarder"

MY_ID = None

if not all([API_ID, API_HASH, BOT_TOKEN, MONGO_URI]):
    raise RuntimeError("API credentials and MONGO_URI must be set in .env file.")

# MongoDB Setup
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client.forwarder_bot
    tasks_collection = db.tasks
    stats_collection = db.stats
    LOGGER.info("Successfully connected to MongoDB.")
except Exception as e:
    LOGGER.error(f"Error connecting to MongoDB: {e}")
    exit(1)

# --- Helper Functions ---

def parse_chat_ids(text: str) -> list[int] | None:
    try:
        return [int(i.strip()) for i in text.split(',')]
    except (ValueError, TypeError):
        return None

def create_beautiful_caption(original_text):
    link_pattern = r'https?://(?:tera[a-z]+|tinyurl|teraboxurl|freeterabox)\.com/\S+'
    links = re.findall(link_pattern, original_text or "")
    if not links: return None
    emojis = random.sample(['😍', '🔥', '❤️', '😈', '💯', '💦', '🔞'], 2)
    caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}"] + [f"V{i}:\n{link}" for i, link in enumerate(links, 1)]
    return "\n\n".join(caption_parts)

async def generate_thumbnail(video_path):
    thumb_path = None
    try:
        thumb_path = os.path.splitext(video_path)[0] + ".jpg"
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened(): return None
        ret, frame = cap.read()
        if not ret: cap.release(); return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        img.thumbnail((320, 320))
        img.save(thumb_path, "JPEG")
        cap.release()
        return thumb_path
    except Exception as e:
        LOGGER.error(f"Thumbnail generation failed: {e}")
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
        return None

def update_stats(task_id: str, success: bool = True):
    try:
        stats_collection.update_one(
            {"task_id": task_id}, 
            {
                "$inc": {"total_forwarded" if success else "total_failed": 1}, 
                "$set": {"last_activity": datetime.utcnow()}
            }, 
            upsert=True
        )
    except Exception as e: LOGGER.error(f"Failed to update stats: {e}")

def apply_text_modifications(text, mods):
    if not text: text = ""
    if mods.get("remove_texts"):
        remove_list = [l.strip() for l in mods["remove_texts"].splitlines() if l.strip()]
        text = "\n".join([line for line in text.splitlines() if line.strip() not in remove_list])
    if mods.get("replace_rules"):
        for rule in mods["replace_rules"].splitlines():
            if '=>' in rule: 
                find, repl = rule.split('=>', 1)
                text = text.replace(find.strip(), repl.strip())
    if mods.get("beautiful_captions"):
        new_caption = create_beautiful_caption(text)
        if new_caption: text = new_caption
    if text: text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if mods.get("footer_text"): text = f"{text or ''}\n\n{mods['footer_text']}"
    return text

# --- Telethon Client (Userbot) ---

ALBUM_BUFFER = {} 
ALBUM_LOCKS = {}

async def process_single_message(dest_id: int, message: Message, caption: str, task_id: str = None):
    path, thumb_path = None, None
    try:
        if message.media:
            # Optimized download
            path = await message.download_media(file=f"temp_single_{message.id}")
            is_video = message.video or (message.document and message.file.mime_type.startswith('video/'))
            if is_video: thumb_path = await generate_thumbnail(path)

        await client.send_file(dest_id, path or message.text, caption=caption, thumb=thumb_path, link_preview=False)
        if task_id: update_stats(task_id, success=True)
    except Exception as e:
        LOGGER.error(f"❌ Failed to copy single message to {dest_id}: {e}")
        if task_id: update_stats(task_id, success=False)
    finally:
        if path and os.path.exists(path): os.remove(path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

async def process_album_batch(task_id, group_id, dest_ids, mods):
    await asyncio.sleep(4) # Wait for all parts
    messages = ALBUM_BUFFER.pop(group_id, [])
    if group_id in ALBUM_LOCKS: del ALBUM_LOCKS[group_id]
    if not messages: return
    
    messages.sort(key=lambda x: x.id)
    paths, thumb_path = [], None
    
    # Find caption
    raw_caption = ""
    for msg in messages:
        if msg.text:
            raw_caption = msg.text
            break
    final_caption = apply_text_modifications(raw_caption, mods)

    try:
        for i, msg in enumerate(messages):
            path = await msg.download_media(file=f"temp_{task_id}_{group_id}_{i}")
            paths.append(path)
            is_video = msg.video or (msg.document and msg.file.mime_type.startswith('video/'))
            if not thumb_path and is_video: thumb_path = await generate_thumbnail(path)
        
        for dest_id in dest_ids:
            await client.send_file(dest_id, paths, caption=final_caption, thumb=thumb_path, link_preview=False)
        update_stats(task_id, success=True)
    except Exception as e:
        LOGGER.error(f"Error processing album {group_id}: {e}")
        update_stats(task_id, success=False)
    finally:
        for path in paths:
            if os.path.exists(path): os.remove(path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

# Initialize Client with optimizations
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID: return
    message = event.message
    chat_id = event.chat_id

    clean_id = int(str(chat_id).replace("-100", ""))
    possible_ids = [chat_id, clean_id, int(f"-100{clean_id}")]

    active_tasks = list(tasks_collection.find({"source_ids": {"$in": possible_ids}, "status": "active"}))
    if not active_tasks: return

    for task in active_tasks:
        block_me = task.get("settings", {}).get("block_me", False)
        if block_me and message.sender_id == task.get("owner_id") and not message.reply_to: continue

        filters_doc = task.get("filters", {})
        msg_text = message.text or ""
        is_video = message.video or (message.document and message.file.mime_type.startswith('video/'))
        
        if filters_doc.get("block_videos") and is_video: continue
        if filters_doc.get("block_photos") and message.photo: continue
        if filters_doc.get("block_documents") and message.document and not is_video: continue
        if filters_doc.get("block_text") and not message.media: continue

        blacklist = filters_doc.get("blacklist_words")
        if blacklist:
            if any(w.lower() in msg_text.lower() for w in blacklist.splitlines() if w.strip()): continue
        
        whitelist = filters_doc.get("whitelist_words")
        if whitelist:
             if not any(w.lower() in msg_text.lower() for w in whitelist.splitlines() if w.strip()): continue

        mods = task.get("modifications", {})
        dest_ids = task.get("destination_ids", [])
        
        if message.grouped_id:
            group_id = message.grouped_id
            if group_id not in ALBUM_BUFFER: ALBUM_BUFFER[group_id] = []
            ALBUM_BUFFER[group_id].append(message)
            if group_id not in ALBUM_LOCKS:
                ALBUM_LOCKS[group_id] = asyncio.create_task(process_album_batch(task['_id'], group_id, dest_ids, mods))
            continue 
        else:
            final_caption = apply_text_modifications(msg_text, mods)
            for dest_id in dest_ids:
                await process_single_message(dest_id, message, final_caption, task['_id'])
            
            delay = task.get("settings", {}).get("delay", 0)
            if delay > 0: await asyncio.sleep(delay)

# --- Telegram Bot (Controller) ---

(ASK_LABEL, ASK_SOURCE, ASK_DESTINATION, ASK_FOOTER, ASK_REPLACE, ASK_REMOVE, ASK_BLACKLIST, ASK_WHITELIST, ASK_DELAY) = range(9)
(MAIN_MENU, SETTINGS_MENU, GET_LINKS, GET_BATCH_DESTINATION, CLONE_SOURCE, CLONE_DEST, CLONE_RESTRICTED) = range(9, 16)

async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, from_cancel=False):
    user_id = update.effective_user.id
    tasks = list(tasks_collection.find({"owner_id": user_id}))
    buttons = []
    for t in tasks:
        status_emoji = '✅' if t.get('status') == 'active' else '❌'
        buttons.append([
            InlineKeyboardButton(f"{status_emoji} {t['_id']}", callback_data=f"toggle_status:{t['_id']}"),
            InlineKeyboardButton("⚙️", callback_data=f"settings_menu:{t['_id']}"),
            InlineKeyboardButton("📊", callback_data=f"view_stats:{t['_id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"delete_confirm:{t['_id']}")
        ])
    keyboard = InlineKeyboardMarkup([*buttons, [InlineKeyboardButton("➕ Create New Task", callback_data="new_task_start")]])
    text = "🔄 *Your Forwarding Tasks:*" if tasks else "You have no tasks. Create one to get started!"
    
    if update.callback_query and not from_cancel:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=keyboard, parse_mode='Markdown')
    return MAIN_MENU

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, value = query.data.partition(':')
    user_id = update.effective_user.id
    
    if action == "toggle_status":
        task = tasks_collection.find_one({"_id": value, "owner_id": user_id})
        if task:
            new_status = "stopped" if task.get('status') == 'active' else 'active'
            tasks_collection.update_one({"_id": value}, {"$set": {"status": new_status}})
            status_text = "▶️ activated" if new_status == "active" else "⏸️ paused"
            await query.answer(f"Task {status_text}!", show_alert=True)
        return await forward_command_handler(update, context)
    elif action == "delete_confirm":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_execute:{value}")], [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main_menu")]])
        await query.edit_message_text(f"⚠️ Delete task '*{value}*'?\nThis cannot be undone.", reply_markup=keyboard, parse_mode='Markdown')
        return MAIN_MENU
    elif action == "delete_execute":
        tasks_collection.delete_one({"_id": value, "owner_id": user_id})
        stats_collection.delete_one({"task_id": value})
        await query.edit_text(f"✅ Task '*{value}*' deleted.", parse_mode='Markdown')
        await asyncio.sleep(2)
        return await forward_command_handler(update, context)
    elif action == "view_stats":
        stats = stats_collection.find_one({"task_id": value})
        if stats:
            text = f"📊 *Stats: {value}*\n✅ Sent: {stats.get('total_forwarded', 0)}\n❌ Failed: {stats.get('total_failed', 0)}\n📅 Last: {stats.get('last_activity', 'Never')}"
        else: text = f"📊 *Stats: {value}*\nNo activity yet."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return MAIN_MENU
    elif query.data == "back_to_main_menu":
        return await forward_command_handler(update, context)
    elif action.startswith("settings_toggle"):
        task_id, filter_type = value.split(":")
        task = tasks_collection.find_one({"_id": task_id, "owner_id": user_id})
        if task:
            if "beautify" in action: db_field = "modifications.beautiful_captions"; current = task.get("modifications", {}).get("beautiful_captions", False)
            elif "blockme" in action: db_field = "settings.block_me"; current = task.get("settings", {}).get("block_me", False)
            else: db_field = f"filters.{filter_type}"; current = task.get("filters", {}).get(filter_type, False)
            tasks_collection.update_one({"_id": task_id}, {"$set": {db_field: not current}})
        context.user_data['current_task_id'] = task_id
        return await show_settings_menu(update, context)
    elif action == "settings_menu":
        context.user_data['current_task_id'] = value
        return await show_settings_menu(update, context)

async def get_chat_titles(ids: list) -> str:
    titles = []
    for chat_id in ids:
        try:
            entity = await client.get_entity(chat_id)
            titles.append(f"• {entity.title} (`{chat_id}`)")
        except Exception: titles.append(f"• Unknown Chat (`{chat_id}`)")
    return "\n".join(titles)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = context.user_data.get('current_task_id')
    task = tasks_collection.find_one({"_id": task_id})
    if not task: await update.callback_query.message.reply_text("❌ Error: Task not found."); return MAIN_MENU

    mods = task.get("modifications", {})
    filters_doc = task.get("filters", {})
    settings = task.get("settings", {})
    
    beautify_emoji = "✅" if mods.get("beautiful_captions") else "❌"
    block_me_emoji = "✅" if settings.get("block_me", False) else "❌"
    def f_emoji(f_type): return "✅" if filters_doc.get(f_type) else "❌"

    text = f"⚙️ *Settings: {task_id}*\n\n📥 *Sources:*\n{await get_chat_titles(task.get('source_ids', []))}\n\n📤 *Destinations:*\n{await get_chat_titles(task.get('destination_ids', []))}\n\n⏱️ *Delay:* {settings.get('delay', 0)}s"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{f_emoji('block_photos')} Photos", callback_data=f"settings_toggle_filter:{task_id}:block_photos"), InlineKeyboardButton(f"{f_emoji('block_videos')} Videos", callback_data=f"settings_toggle_filter:{task_id}:block_videos")],
        [InlineKeyboardButton(f"{f_emoji('block_documents')} Docs", callback_data=f"settings_toggle_filter:{task_id}:block_documents"), InlineKeyboardButton(f"{f_emoji('block_text')} Text", callback_data=f"settings_toggle_filter:{task_id}:block_text")],
        [InlineKeyboardButton("📝 Blacklist", callback_data="settings_edit_blacklist"), InlineKeyboardButton("📝 Whitelist", callback_data="settings_edit_whitelist")],
        [InlineKeyboardButton(f"{beautify_emoji} Beautiful Captions", callback_data=f"settings_toggle_beautify:{task_id}:_"), InlineKeyboardButton(f"{block_me_emoji} Block Me", callback_data=f"settings_toggle_blockme:{task_id}:_")],
        [InlineKeyboardButton("📝 Footer", callback_data="settings_edit_footer"), InlineKeyboardButton("🔄 Replace", callback_data="settings_edit_replace")],
        [InlineKeyboardButton("✂️ Remove Text", callback_data="settings_edit_remove"), InlineKeyboardButton("⏱️ Delay", callback_data="settings_edit_delay")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ])
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else: await context.bot.send_message(update.effective_chat.id, text, reply_markup=keyboard, parse_mode='Markdown')
    return SETTINGS_MENU

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("📝 Enter a unique name for this task.\nOr /cancel."); return ASK_LABEL
async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if tasks_collection.find_one({"_id": label}): await update.message.reply_text("❌ Label exists. Choose another."); return ASK_LABEL
    context.user_data['new_task_label'] = label
    await update.message.reply_text("✅ Label set!\n📥 Send Source Chat ID(s)."); return ASK_SOURCE
async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids: await update.message.reply_text("❌ Invalid ID."); return ASK_SOURCE
    context.user_data['new_task_source'] = ids
    await update.message.reply_text("✅ Source set!\n📤 Send Destination Chat ID(s)."); return ASK_DESTINATION
async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids: await update.message.reply_text("❌ Invalid ID."); return ASK_DESTINATION
    tasks_collection.insert_one({
        "_id": context.user_data['new_task_label'], "owner_id": update.effective_user.id, "status": "active",
        "source_ids": context.user_data['new_task_source'], "destination_ids": ids,
        "modifications": {"footer_text": None, "replace_rules": None, "remove_texts": None, "beautiful_captions": False},
        "filters": {"blacklist_words": None, "whitelist_words": None, "block_photos": False, "block_videos": False, "block_documents": False, "block_text": False},
        "settings": {"delay": 0, "block_me": False}, "created_at": datetime.utcnow()
    })
    context.user_data.clear(); await update.message.reply_text("✅ Task created!"); await forward_command_handler(update, context); return ConversationHandler.END

async def edit_setting_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.callback_query.data; task_id = context.user_data.get('current_task_id')
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"settings_menu:{task_id}")]])
    instr = "\n\n🟢 *Edit:*\n• Send text to **ADD**.\n• Send `-text` to **REMOVE**.\n• Send `/clear` to **DELETE ALL**."
    
    if action == "settings_edit_footer": text = f"📝 *Footer*\nSend new footer.\n`/clear` to remove."
    elif action == "settings_edit_replace": text = f"🔄 *Replace*\n`old => new`\n{instr}"
    elif action == "settings_edit_remove": text = f"✂️ *Remove Lines*\n{instr}"
    elif action == "settings_edit_blacklist": text = f"🚫 *Blacklist*\n{instr}"
    elif action == "settings_edit_whitelist": text = f"✅ *Whitelist*\n{instr}"
    elif action == "settings_edit_delay": text = f"⏱️ *Delay*\nSend seconds (0 to disable)."
    else: return SETTINGS_MENU
    
    await update.callback_query.edit_message_text(text, reply_markup=back_kb, parse_mode='Markdown')
    return {"settings_edit_footer": ASK_FOOTER, "settings_edit_replace": ASK_REPLACE, "settings_edit_remove": ASK_REMOVE, "settings_edit_blacklist": ASK_BLACKLIST, "settings_edit_whitelist": ASK_WHITELIST, "settings_edit_delay": ASK_DELAY}[action]

async def save_setting_text(update: Update, context: ContextTypes.DEFAULT_TYPE, db_key_path: str):
    task_id = context.user_data.get('current_task_id'); user_text = update.message.text.strip()
    task = tasks_collection.find_one({"_id": task_id})
    
    if "footer_text" in db_key_path:
        new_value = None if user_text in ['/clear', '/skip'] else user_text
        msg = "✅ Footer updated." if new_value else "🗑️ Footer deleted."
    else:
        keys = db_key_path.split('.'); current_value = task
        for k in keys: current_value = current_value.get(k, {})
        current_lines = [line.strip() for line in (current_value or "").split('\n') if line.strip()]
        
        if user_text in ['/clear', '/skip']: new_value = ""; msg = "🗑️ List cleared."
        elif user_text.startswith('-'):
            item = user_text[1:].strip()
            if item in current_lines: current_lines.remove(item); new_value = "\n".join(current_lines); msg = f"🗑️ Removed: '{item}'"
            else: new_value = "\n".join(current_lines); msg = f"❌ Not found: '{item}'"
        else:
            if user_text not in current_lines: current_lines.append(user_text); new_value = "\n".join(current_lines); msg = f"✅ Added: '{user_text}'"
            else: new_value = "\n".join(current_lines); msg = "⚠️ Exists."

    tasks_collection.update_one({"_id": task_id}, {"$set": {db_key_path: new_value}})
    await update.message.reply_text(msg); await asyncio.sleep(1); return await show_settings_menu(update, context)

async def get_footer(u, c): return await save_setting_text(u, c, "modifications.footer_text")
async def get_replace_rules(u, c): return await save_setting_text(u, c, "modifications.replace_rules")
async def get_remove_texts(u, c): return await save_setting_text(u, c, "modifications.remove_texts")
async def get_blacklist(u, c): return await save_setting_text(u, c, "filters.blacklist_words")
async def get_whitelist(u, c): return await save_setting_text(u, c, "filters.whitelist_words")
async def get_delay(u, c):
    try:
        val = int(u.message.text.strip())
        tasks_collection.update_one({"_id": c.user_data['current_task_id']}, {"$set": {"settings.delay": val}})
        await u.message.reply_text(f"✅ Delay: {val}s")
    except: await u.message.reply_text("❌ Invalid number.")
    await asyncio.sleep(1); return await show_settings_menu(u, c)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("❌ Cancelled."); await forward_command_handler(update, context, from_cancel=True); return ConversationHandler.END

# --- Batch & Clone ---
async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📦 *Batch Mode*\nSend start and end links.\nExample:\n`https://t.me/channel/100`\n`https://t.me/channel/120`", parse_mode='Markdown'); return GET_LINKS

async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip(); links = text.split()
    if len(links) < 2: await update.message.reply_text("❌ Send exactly two links (Start & End)."); return GET_LINKS
    
    def parse_link(l):
        # Support Public (t.me/user/123) and Private (t.me/c/12345/123)
        m = re.match(r"https?://t\.me/(?:c/)?(\w+)/(\d+)", l)
        if not m: return None, None
        chat_id_str, msg_id = m.groups()
        if chat_id_str.isdigit(): return int(f"-100{chat_id_str}"), int(msg_id) # Private
        return chat_id_str, int(msg_id) # Public (username)

    s_c, s_id = parse_link(links[0]); e_c, e_id = parse_link(links[1])
    
    if not s_c or not e_c: await update.message.reply_text("❌ Invalid link format."); return GET_LINKS
    # Note: We don't strictly check s_c == e_c because username vs ID might differ but be same chat.
    
    context.user_data['batch_info'] = {'channel_id': s_c, 'start_id': s_id, 'end_id': e_id}
    await update.message.reply_text("✅ Links valid!\n📤 Send Destination Chat ID."); return GET_BATCH_DESTINATION

async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = parse_chat_ids(update.message.text)
    if not ids: await update.message.reply_text("❌ Invalid destination."); return GET_BATCH_DESTINATION
    info, dest = context.user_data['batch_info'], ids[0]
    status_msg = await update.message.reply_text(f"⏳ Batching {info['start_id']} -> {info['end_id']}...")
    try:
        msgs = await client.get_messages(info['channel_id'], ids=range(info['start_id'], info['end_id']+1))
        valid_msgs = [m for m in msgs if m]
        await status_msg.edit_text(f"✅ Found {len(valid_msgs)} messages. Processing...")
        for m in valid_msgs:
            if m.grouped_id: 
                # Simple handling for batch albums: just send them. 
                # (Full album logic is complex for batch, usually sending single is safer or using forward)
                await process_single_message(dest, m, m.text) 
            else:
                await process_single_message(dest, m, m.text)
            await asyncio.sleep(2)
        await status_msg.edit_text("✅ Batch complete!")
    except Exception as e: await status_msg.edit_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def clone_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📋 *Clone Mode*\n📥 Send Source Channel ID.", parse_mode='Markdown'); return CLONE_SOURCE
async def clone_get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = parse_chat_ids(update.message.text)
    if not ids: await update.message.reply_text("❌ Invalid ID."); return CLONE_SOURCE
    context.user_data['clone_source'] = ids[0]; await update.message.reply_text("✅ Source set!\n📤 Send Destination ID."); return CLONE_DEST
async def clone_get_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = parse_chat_ids(update.message.text)
    if not ids: await update.message.reply_text("❌ Invalid ID."); return CLONE_DEST
    context.user_data['clone_dest'] = ids[0]
    await update.message.reply_text("⚙️ Restricted content? (Yes/No)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes (Download)", callback_data="clone_restricted:true"), InlineKeyboardButton("No (Forward)", callback_data="clone_restricted:false")]]))
    return CLONE_RESTRICTED
async def clone_set_restricted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['clone_restricted'] = update.callback_query.data.split(':')[1] == 'true'
    await update.callback_query.edit_message_text("⏳ Send 'done' to start or links to skip."); return await clone_get_skip_links(update, context)
async def clone_get_skip_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault('clone_skip_ids', []); return CLONE_RESTRICTED
async def clone_process_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.lower() == 'done': return await clone_execute(update, context)
    m = re.match(r"https?://t\.me/(?:c/)?(\w+)/(\d+)", update.message.text)
    if m: context.user_data['clone_skip_ids'].append(int(m.group(2))); await update.message.reply_text(f"Skipped {m.group(2)}")
    return CLONE_RESTRICTED
async def clone_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    src, dst, restr = context.user_data['clone_source'], context.user_data['clone_dest'], context.user_data.get('clone_restricted', False)
    skips = set(context.user_data.get('clone_skip_ids', []))
    msg = await update.message.reply_text("⏳ Fetching...")
    try:
        msgs = []
        async for m in client.iter_messages(src):
            if m.id not in skips: msgs.append(m)
        msgs.reverse(); await msg.edit_text(f"✅ Cloning {len(msgs)} messages...")
        for m in msgs:
            try:
                if restr: await process_single_message(dst, m, m.text)
                else: await client.forward_messages(dst, m.id, src)
                await asyncio.sleep(2)
            except Exception as e: LOGGER.error(f"Clone err: {e}")
        await msg.edit_text("✅ Done!")
    except Exception as e: await msg.edit_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def auto_save_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"https?://t\.me/(?:c/)?(\w+)/(\d+)", update.message.text)
    if not m: return
    chat_id_str, msg_id = m.groups()
    chat_id = int(f"-100{chat_id_str}") if chat_id_str.isdigit() else chat_id_str
    status = await update.message.reply_text("⏳ Saving...")
    try:
        msg = await client.get_messages(chat_id, ids=int(msg_id))
        if not msg: await status.edit_text("❌ Not found."); return
        await process_single_message(update.effective_user.id, msg, msg.text)
        await status.edit_text("✅ Saved!")
    except Exception as e: await status.edit_text(f"❌ Error: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 *Welcome!*\n/forward - Tasks\n/batch - Batch Copy\n/clone - Clone Channel\n/help - Info", parse_mode='Markdown')
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 *Help*\n/forward - Create/Manage Tasks\n/batch - Copy range of messages\n/clone - Copy full channel", parse_mode='Markdown')

async def main():
    global MY_ID
    application = Application.builder().token(BOT_TOKEN).build()
    cancel_handler = CommandHandler('cancel', cancel)
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("forward", forward_command_handler), CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|settings_menu|settings_toggle|view_stats)"), CallbackQueryHandler(new_task_start, pattern="^new_task_start$")],
        states={
            MAIN_MENU: [CommandHandler("forward", forward_command_handler), CallbackQueryHandler(new_task_start, pattern="^new_task_start$"), CallbackQueryHandler(callback_query_handler)],
            SETTINGS_MENU: [CommandHandler("forward", forward_command_handler), CallbackQueryHandler(edit_setting_ask, pattern="^settings_edit_"), CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$"), CallbackQueryHandler(callback_query_handler)],
            ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)], ASK_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_source)], ASK_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_destination)],
            ASK_FOOTER: [MessageHandler(filters.TEXT, get_footer), CallbackQueryHandler(callback_query_handler)], ASK_REPLACE: [MessageHandler(filters.TEXT, get_replace_rules), CallbackQueryHandler(callback_query_handler)],
            ASK_REMOVE: [MessageHandler(filters.TEXT, get_remove_texts), CallbackQueryHandler(callback_query_handler)], ASK_BLACKLIST: [MessageHandler(filters.TEXT, get_blacklist), CallbackQueryHandler(callback_query_handler)],
            ASK_WHITELIST: [MessageHandler(filters.TEXT, get_whitelist), CallbackQueryHandler(callback_query_handler)], ASK_DELAY: [MessageHandler(filters.TEXT, get_delay), CallbackQueryHandler(callback_query_handler)]
        }, fallbacks=[cancel_handler], allow_reentry=True
    )
    
    batch_conv = ConversationHandler(entry_points=[CommandHandler('batch', batch_start)], states={GET_LINKS: [MessageHandler(filters.TEXT, get_links)], GET_BATCH_DESTINATION: [MessageHandler(filters.ALL, get_batch_destination)]}, fallbacks=[cancel_handler], allow_reentry=True)
    clone_conv = ConversationHandler(entry_points=[CommandHandler('clone', clone_start)], states={CLONE_SOURCE: [MessageHandler(filters.ALL, clone_get_source)], CLONE_DEST: [MessageHandler(filters.ALL, clone_get_dest)], CLONE_RESTRICTED: [CallbackQueryHandler(clone_set_restricted), MessageHandler(filters.TEXT, clone_process_skip)]}, fallbacks=[cancel_handler], allow_reentry=True)

    application.add_handler(conv_handler); application.add_handler(batch_conv); application.add_handler(clone_conv)
    application.add_handler(MessageHandler(filters.Regex(r'https?://t\.me/') & filters.TEXT, auto_save_handler))
    application.add_handler(CommandHandler("start", start_command)); application.add_handler(CommandHandler("help", help_command))

    LOGGER.info("Bot starting..."); await application.initialize(); await application.start(); await application.updater.start_polling()
    await client.start(); me = await client.get_me(); MY_ID = me.id; LOGGER.info(f"Telethon: {me.first_name}"); await client.run_until_disconnected(); await application.stop()

if __name__ == "__main__": asyncio.run(main())