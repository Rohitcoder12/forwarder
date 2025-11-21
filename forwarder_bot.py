import asyncio
import os
import re
import random
import logging
from dotenv import load_dotenv
import cv2
from PIL import Image
from telethon import TelegramClient, events
from telethon.tl.types import Message
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

# --- Telethon Client (Userbot) ---

ALBUM_HANDLING_TASKS = {}

async def process_single_message(dest_id: int, message: Message, caption: str, task_id: str = None):
    path, thumb_path = None, None
    try:
        if message.media:
            path = await message.download_media(file=f"temp_single_{message.id}")
            if message.video: thumb_path = await generate_thumbnail(path)
        
        await client.send_file(dest_id, path or message.text, caption=caption, thumb=thumb_path, link_preview=False)
        if task_id: update_stats(task_id, success=True)
    except Exception as e:
        LOGGER.error(f"Failed to copy single message to {dest_id}: {e}")
        if task_id: update_stats(task_id, success=False)
    finally:
        if path and os.path.exists(path): os.remove(path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

async def process_album(task_id, group_id, dest_ids, caption):
    await asyncio.sleep(3)
    album_key = f"{task_id}_{group_id}"
    messages = ALBUM_HANDLING_TASKS.pop(album_key, [])
    if not messages: return
    
    paths, thumb_path = [], None
    try:
        for i, msg in enumerate(messages):
            path = await msg.download_media(file=f"temp_{task_id}_{group_id}_{i}")
            paths.append(path)
            if not thumb_path and msg.video: thumb_path = await generate_thumbnail(path)
        
        for dest_id in dest_ids:
            await client.send_file(dest_id, paths, caption=caption, thumb=thumb_path, link_preview=False)
        update_stats(task_id, success=True)
    except Exception as e:
        LOGGER.error(f"Error processing album {group_id}: {e}")
        update_stats(task_id, success=False)
    finally:
        for path in paths:
            if os.path.exists(path): os.remove(path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID: return
    message = event.message
    
    # Find tasks where the source matches the chat ID
    active_tasks = tasks_collection.find({"source_ids": event.chat_id, "status": "active"})
    
    for task in active_tasks:
        # Block Me Check
        block_me = task.get("settings", {}).get("block_me", False)
        if block_me and message.sender_id == task.get("owner_id"):
            if not message.reply_to: continue

        # Filters
        filters_doc = task.get("filters", {})
        msg_text = message.text or ""
        
        if (filters_doc.get("block_photos") and message.photo) or \
           (filters_doc.get("block_videos") and message.video) or \
           (filters_doc.get("block_documents") and message.document) or \
           (filters_doc.get("block_text") and not message.media): 
            continue

        # Text Filters (Blacklist/Whitelist)
        blacklist = filters_doc.get("blacklist_words")
        if blacklist:
            # Handle cases where blacklist might be empty string in DB
            blacklist_lines = [w for w in blacklist.splitlines() if w.strip()]
            if any(word.lower() in msg_text.lower() for word in blacklist_lines): continue
        
        whitelist = filters_doc.get("whitelist_words")
        if whitelist:
             whitelist_lines = [w for w in whitelist.splitlines() if w.strip()]
             # If whitelist exists, message MUST contain at least one word
             if whitelist_lines and not any(word.lower() in msg_text.lower() for word in whitelist_lines):
                 continue

        # Modifications
        mods = task.get("modifications", {})
        final_caption = msg_text
        
        if mods.get("remove_texts"):
            remove_list = [l.strip() for l in mods["remove_texts"].splitlines() if l.strip()]
            final_caption = "\n".join([line for line in final_caption.splitlines() if line.strip() not in remove_list])
            
        if mods.get("replace_rules"):
            for rule in mods["replace_rules"].splitlines():
                if '=>' in rule: 
                    find, repl = rule.split('=>', 1)
                    final_caption = final_caption.replace(find.strip(), repl.strip())
                    
        if mods.get("beautiful_captions"):
            new_caption = create_beautiful_caption(final_caption)
            if new_caption: final_caption = new_caption
            
        if final_caption: 
            final_caption = re.sub(r'\n{3,}', '\n\n', final_caption).strip()
            
        if mods.get("footer_text"): 
            final_caption = f"{final_caption or ''}\n\n{mods['footer_text']}"

        # Sending
        dest_ids = task.get("destination_ids", [])
        
        if message.grouped_id:
            album_key = f"{task['_id']}_{message.grouped_id}"
            if album_key not in ALBUM_HANDLING_TASKS:
                ALBUM_HANDLING_TASKS[album_key] = []
                asyncio.create_task(process_album(task['_id'], message.grouped_id, dest_ids, final_caption))
            ALBUM_HANDLING_TASKS[album_key].append(message)
        else:
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_execute:{value}")], 
            [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main_menu")]
        ])
        await query.edit_message_text(f"⚠️ Are you sure you want to delete task '*{value}*'?\n\nThis action cannot be undone.", reply_markup=keyboard, parse_mode='Markdown')
        return MAIN_MENU
        
    elif action == "delete_execute":
        result = tasks_collection.delete_one({"_id": value, "owner_id": user_id})
        if result.deleted_count > 0:
            stats_collection.delete_one({"task_id": value})
            await query.edit_text(f"✅ Task '*{value}*' has been deleted successfully.", parse_mode='Markdown')
            await asyncio.sleep(2)
            return await forward_command_handler(update, context)
        else:
            await query.edit_text("❌ Task not found or already deleted.")
            return MAIN_MENU
            
    elif action == "view_stats":
        stats = stats_collection.find_one({"task_id": value})
        if stats:
            total = stats.get("total_forwarded", 0)
            failed = stats.get("total_failed", 0)
            last_activity = stats.get("last_activity", "Never")
            text = f"📊 *Statistics for: {value}*\n\n✅ Successfully forwarded: {total}\n❌ Failed: {failed}\n📅 Last activity: {last_activity}"
        else:
            text = f"📊 *Statistics for: {value}*\n\nNo activity yet."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return MAIN_MENU
        
    elif query.data == "back_to_main_menu":
        return await forward_command_handler(update, context)
        
    elif action.startswith("settings_toggle"):
        # Toggle booleans
        task_id, filter_type = value.split(":")
        task = tasks_collection.find_one({"_id": task_id, "owner_id": user_id})
        if task:
            if "beautify" in action:
                db_field = "modifications.beautiful_captions"
                current_status = task.get("modifications", {}).get("beautiful_captions", False)
            elif "blockme" in action:
                db_field = "settings.block_me"
                current_status = task.get("settings", {}).get("block_me", False)
            else:
                db_field = f"filters.{filter_type}"
                current_status = task.get("filters", {}).get(filter_type, False)
            
            tasks_collection.update_one({"_id": task_id}, {"$set": {db_field: not current_status}})
        
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
        except Exception:
            titles.append(f"• Unknown Chat (`{chat_id}`)")
    return "\n".join(titles)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = context.user_data.get('current_task_id')
    task = tasks_collection.find_one({"_id": task_id})
    if not task:
        await (update.callback_query.message if update.callback_query else update.message).reply_text("❌ Error: Task not found.")
        return MAIN_MENU

    mods = task.get("modifications", {})
    filters_doc = task.get("filters", {})
    settings = task.get("settings", {})
    
    beautify_emoji = "✅" if mods.get("beautiful_captions") else "❌"
    block_me_emoji = "✅" if settings.get("block_me", False) else "❌"
    def f_emoji(f_type): return "✅" if filters_doc.get(f_type) else "❌"

    source_info = await get_chat_titles(task.get('source_ids', []))
    dest_info = await get_chat_titles(task.get('destination_ids', []))
    delay = settings.get('delay', 0)

    text = f"⚙️ *Settings for: {task_id}*\n\n📥 *Sources:*\n{source_info}\n\n📤 *Destinations:*\n{dest_info}\n\n⏱️ *Delay:* {delay}s"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{f_emoji('block_photos')} Photos", callback_data=f"settings_toggle_filter:{task_id}:block_photos"), 
         InlineKeyboardButton(f"{f_emoji('block_videos')} Videos", callback_data=f"settings_toggle_filter:{task_id}:block_videos")],
        [InlineKeyboardButton(f"{f_emoji('block_documents')} Docs", callback_data=f"settings_toggle_filter:{task_id}:block_documents"), 
         InlineKeyboardButton(f"{f_emoji('block_text')} Text", callback_data=f"settings_toggle_filter:{task_id}:block_text")],
        [InlineKeyboardButton("📝 Blacklist", callback_data="settings_edit_blacklist"), 
         InlineKeyboardButton("📝 Whitelist", callback_data="settings_edit_whitelist")],
        [InlineKeyboardButton(f"{beautify_emoji} Beautiful Captions", callback_data=f"settings_toggle_beautify:{task_id}:_"),
         InlineKeyboardButton(f"{block_me_emoji} Block Me", callback_data=f"settings_toggle_blockme:{task_id}:_")],
        [InlineKeyboardButton("📝 Footer", callback_data="settings_edit_footer"), 
         InlineKeyboardButton("🔄 Replace", callback_data="settings_edit_replace")],
        [InlineKeyboardButton("✂️ Remove Text", callback_data="settings_edit_remove"), 
         InlineKeyboardButton("⏱️ Delay", callback_data="settings_edit_delay")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=keyboard, parse_mode='Markdown')
        
    return SETTINGS_MENU

# --- Task Creation Wizard ---

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("📝 Please provide a unique name for this task.\n\nOr /cancel to abort.")
    return ASK_LABEL

async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if tasks_collection.find_one({"_id": label}):
        await update.message.reply_text("❌ This label already exists. Please choose another or /cancel.")
        return ASK_LABEL
    context.user_data['new_task_label'] = label
    await update.message.reply_text("✅ Label set!\n\n📥 Now send Source Chat ID(s) (comma-separated) or forward a message from the source channel.\n\nOr /cancel.")
    return ASK_SOURCE

async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.forward_origin:
        ids = [update.message.forward_origin.chat.id]
    else:
        ids = parse_chat_ids(update.message.text)
    
    if not ids:
        await update.message.reply_text("❌ Invalid ID. Please send numeric IDs or forward a message. Or /cancel.")
        return ASK_SOURCE
    
    context.user_data['new_task_source'] = ids
    await update.message.reply_text("✅ Source(s) set!\n\n📤 Now send Destination Chat ID(s) (comma-separated) or forward a message from the destination channel.\n\nOr /cancel.")
    return ASK_DESTINATION

async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.forward_origin:
        ids = [update.message.forward_origin.chat.id]
    else:
        ids = parse_chat_ids(update.message.text)
        
    if not ids:
        await update.message.reply_text("❌ Invalid ID. Please send numeric IDs or forward a message. Or /cancel.")
        return ASK_DESTINATION
        
    tasks_collection.insert_one({
        "_id": context.user_data['new_task_label'],
        "owner_id": update.effective_user.id,
        "status": "active",
        "source_ids": context.user_data['new_task_source'],
        "destination_ids": ids,
        "modifications": {"footer_text": None, "replace_rules": None, "remove_texts": None, "beautiful_captions": False},
        "filters": {"blacklist_words": None, "whitelist_words": None, "block_photos": False, "block_videos": False, "block_documents": False, "block_text": False},
        "settings": {"delay": 0, "block_me": False},
        "created_at": datetime.utcnow()
    })
    context.user_data.clear()
    await update.message.reply_text("✅ Task created successfully!")
    await forward_command_handler(update, context)
    return ConversationHandler.END

# --- Settings Editors (Fixed Logic) ---

async def edit_setting_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.callback_query.data
    task_id = context.user_data.get('current_task_id')
    task = tasks_collection.find_one({"_id": task_id})
    
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data=f"settings_menu:{task_id}")]])
    
    instructions = (
        "\n\n🟢 *How to edit:*\n"
        "• Send text to **ADD** it to the list.\n"
        "• Send text starting with `-` to **REMOVE** a specific line (e.g., `-badword`).\n"
        "• Send `/clear` to **DELETE ALL**."
    )

    if action == "settings_edit_footer":
        current = task.get("modifications", {}).get("footer_text", "")
        current_display = f"```\n{current}\n```" if current else "_None_"
        text = f"📝 *Footer Text*\n\nCurrently set to:\n{current_display}\n\nSend the new footer text (this replaces the old one).\nOr `/clear` to remove."
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_FOOTER
    
    elif action == "settings_edit_replace":
        current = task.get("modifications", {}).get("replace_rules", "")
        current_display = f"```\n{current}\n```" if current else "_None_"
        text = f"🔄 *Replace Rules*\nFormat: `old => new`\n\nCurrent Rules:\n{current_display}{instructions}"
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_REPLACE
    
    elif action == "settings_edit_remove":
        current = task.get("modifications", {}).get("remove_texts", "")
        current_display = f"```\n{current}\n```" if current else "_None_"
        text = f"✂️ *Remove Lines Containing*\n\nCurrent List:\n{current_display}{instructions}"
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_REMOVE
    
    elif action == "settings_edit_blacklist":
        current = task.get("filters", {}).get("blacklist_words", "")
        current_display = f"```\n{current}\n```" if current else "_None_"
        text = f"🚫 *Blacklist Words*\n\nCurrent List:\n{current_display}{instructions}"
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_BLACKLIST
    
    elif action == "settings_edit_whitelist":
        current = task.get("filters", {}).get("whitelist_words", "")
        current_display = f"```\n{current}\n```" if current else "_None_"
        text = f"✅ *Whitelist Words*\n\nCurrent List:\n{current_display}{instructions}"
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_WHITELIST
    
    elif action == "settings_edit_delay":
        current_delay = task.get("settings", {}).get("delay", 0)
        text = f"⏱️ *Current Delay:* {current_delay}s\n\nSend delay in seconds (0 to disable).\n\nOr /cancel."
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        return ASK_DELAY
    
    return SETTINGS_MENU

async def save_setting_text(update: Update, context: ContextTypes.DEFAULT_TYPE, db_key_path: str):
    task_id = context.user_data.get('current_task_id')
    if not task_id: return ConversationHandler.END
    
    user_text = update.message.text.strip()
    task = tasks_collection.find_one({"_id": task_id})
    
    # 1. Logic for Footer (It's a single string, not a list)
    if "footer_text" in db_key_path:
        if user_text == '/clear' or user_text == '/skip':
            new_value = None
            msg = "🗑️ Footer deleted."
        else:
            new_value = user_text
            msg = "✅ Footer updated."
    
    # 2. Logic for Lists (Append/Remove)
    else:
        # Drill down to get current value
        keys = db_key_path.split('.')
        current_value = task
        for k in keys:
            current_value = current_value.get(k, {})
        
        if not isinstance(current_value, str): current_value = ""
        current_lines = [line.strip() for line in current_value.split('\n') if line.strip()]
        
        if user_text == '/clear' or user_text == '/skip':
            # FIX: Use empty string "" instead of None to prevent errors next time
            new_value = "" 
            msg = "🗑️ List cleared successfully."
        
        elif user_text.startswith('-'):
            # REMOVE specific item
            item_to_remove = user_text[1:].strip()
            if item_to_remove in current_lines:
                current_lines.remove(item_to_remove)
                new_value = "\n".join(current_lines)
                msg = f"🗑️ Removed: '{item_to_remove}'"
            else:
                new_value = "\n".join(current_lines)
                msg = f"❌ Could not find '{item_to_remove}' in the list."
        else:
            # APPEND new item
            if user_text not in current_lines:
                current_lines.append(user_text)
                new_value = "\n".join(current_lines)
                msg = f"✅ Added: '{user_text}'"
            else:
                new_value = "\n".join(current_lines)
                msg = "⚠️ Item already exists in list."

    tasks_collection.update_one({"_id": task_id}, {"$set": {db_key_path: new_value}})
    await update.message.reply_text(msg)
    
    context.user_data['current_task_id'] = task_id
    await asyncio.sleep(1)
    return await show_settings_menu(update, context)

async def get_footer(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await save_setting_text(update, context, "modifications.footer_text")

async def get_replace_rules(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await save_setting_text(update, context, "modifications.replace_rules")

async def get_remove_texts(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await save_setting_text(update, context, "modifications.remove_texts")

async def get_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await save_setting_text(update, context, "filters.blacklist_words")

async def get_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await save_setting_text(update, context, "filters.whitelist_words")

async def get_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = context.user_data.get('current_task_id')
    try:
        delay = int(update.message.text.strip())
        if delay < 0: raise ValueError
        tasks_collection.update_one({"_id": task_id}, {"$set": {"settings.delay": delay}})
        await update.message.reply_text(f"✅ Delay set to {delay} seconds!")
    except ValueError:
        await update.message.reply_text("❌ Please send a valid number (0 or more).")
    
    context.user_data['current_task_id'] = task_id
    await asyncio.sleep(1)
    return await show_settings_menu(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Operation cancelled.")
    await forward_command_handler(update, context, from_cancel=True)
    return ConversationHandler.END

# --- Auto Save Handler ---
async def auto_save_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    match = re.match(r"https?://t\.me/(c/)?(\w+)/(\d+)", message_text)
    if not match: return

    status_msg = await update.message.reply_text("⏳ Fetching post...")
    path, thumb_path = None, None
    try:
        chat_id = int(f"-100{match.group(2)}") if match.group(1) else match.group(2)
        msg_id = int(match.group(3))
        message = await client.get_messages(chat_id, ids=msg_id)
        
        if not message: 
            await status_msg.edit_text("❌ Could not fetch message.")
            return

        if message.media:
            path = await message.download_media(file=f"temp_save_{message.id}")
            if message.video: thumb_path = await generate_thumbnail(path)
        
        user_chat_id = update.effective_user.id
        if path:
            with open(path, 'rb') as file:
                if message.photo: 
                    await context.bot.send_photo(chat_id=user_chat_id, photo=file, caption=message.text or "")
                elif message.video:
                    thumb_file = open(thumb_path, 'rb') if thumb_path else None
                    await context.bot.send_video(chat_id=user_chat_id, video=file, thumbnail=thumb_file, caption=message.text or "")
                    if thumb_file: thumb_file.close()
                else: 
                    await context.bot.send_document(chat_id=user_chat_id, document=file, caption=message.text or "")
        elif message.text: 
            await context.bot.send_message(chat_id=user_chat_id, text=message.text, disable_web_page_preview=True)
            
        await status_msg.edit_text("✅ Post saved!")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
    finally:
        if path and os.path.exists(path): os.remove(path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

# --- Clone Handler ---
async def clone_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📋 *Clone Channel Mode*\n\n📥 Send source channel ID or forward a message from source channel.\n\nOr /cancel to abort.", parse_mode='Markdown')
    return CLONE_SOURCE

async def clone_get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.forward_origin: source_id = update.message.forward_origin.chat.id
    else:
        parsed = parse_chat_ids(update.message.text)
        source_id = parsed[0] if parsed else None
    if not source_id: await update.message.reply_text("❌ Invalid source. Try again or /cancel."); return CLONE_SOURCE
    context.user_data['clone_source'] = source_id
    await update.message.reply_text("✅ Source set!\n\n📤 Now send destination channel ID or forward a message from destination.\n\nOr /cancel.")
    return CLONE_DEST

async def clone_get_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.forward_origin: dest_id = update.message.forward_origin.chat.id
    else:
        parsed = parse_chat_ids(update.message.text)
        dest_id = parsed[0] if parsed else None
    if not dest_id: await update.message.reply_text("❌ Invalid destination. Try again or /cancel."); return CLONE_DEST
    context.user_data['clone_dest'] = dest_id
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Forwarding Allowed", callback_data="clone_restricted:false")], [InlineKeyboardButton("🚫 Restricted (Download & Upload)", callback_data="clone_restricted:true")]])
    await update.message.reply_text("⚙️ *Channel Settings*\n\nIs forwarding restricted in the source channel?", reply_markup=keyboard, parse_mode='Markdown')
    return CLONE_RESTRICTED

async def clone_set_restricted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    restricted = query.data.split(':')[1] == 'true'
    context.user_data['clone_restricted'] = restricted
    source_id = context.user_data['clone_source']
    dest_id = context.user_data['clone_dest']
    await query.edit_message_text("⏳ Starting clone process...\n\nSend message links to skip specific messages, or send 'done' to start cloning.")
    return await clone_get_skip_links(update, context)

async def clone_get_skip_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault('clone_skip_ids', [])
    return CLONE_RESTRICTED

async def clone_process_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.lower() == 'done':
        return await clone_execute(update, context)
    match = re.match(r"https?://t\.me/(c/)?(\w+)/(\d+)", update.message.text)
    if match:
        msg_id = int(match.group(3))
        context.user_data['clone_skip_ids'].append(msg_id)
        await update.message.reply_text(f"✅ Message {msg_id} will be skipped.\n\nSend more links or 'done' to start.")
        return CLONE_RESTRICTED
    await update.message.reply_text("❌ Invalid link. Send a valid message link or 'done'.")
    return CLONE_RESTRICTED

async def clone_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    source_id = context.user_data['clone_source']
    dest_id = context.user_data['clone_dest']
    restricted = context.user_data.get('clone_restricted', False)
    skip_ids = set(context.user_data.get('clone_skip_ids', []))
    status_msg = await update.message.reply_text("⏳ Fetching all messages from source channel...")
    count, errors, skipped = 0, 0, 0
    try:
        all_messages = []
        async for message in client.iter_messages(source_id):
            if message.id not in skip_ids:
                all_messages.append(message)
            else:
                skipped += 1
        all_messages.reverse()
        total = len(all_messages)
        await status_msg.edit_text(f"✅ Found {total} messages to clone (skipped {skipped}). Starting...")
        for idx, message in enumerate(all_messages):
            try:
                if (idx + 1) % 10 == 0:
                    await status_msg.edit_text(f"⏳ Progress: {idx+1}/{total} messages processed...")
                if restricted:
                    if message.grouped_id:
                        album = [m for m in all_messages[idx:] if m and m.grouped_id == message.grouped_id]
                        album_paths, thumb_path = [], None
                        try:
                            for j, msg in enumerate(album):
                                path = await msg.download_media(file=f"temp_clone_{msg.id}_{j}")
                                if path: album_paths.append(path)
                                if not thumb_path and msg.video: thumb_path = await generate_thumbnail(path)
                            if album_paths:
                                await client.send_file(dest_id, album_paths, caption=album[0].text, thumb=thumb_path)
                                count += len(album)
                        except Exception as e:
                            LOGGER.error(f"Clone album error: {e}")
                            errors += len(album)
                        finally:
                            for path in album_paths:
                                if path and os.path.exists(path): os.remove(path)
                            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
                    else:
                        path, thumb_path = None, None
                        try:
                            if message.media:
                                path = await message.download_media(file=f"temp_clone_{message.id}")
                                if message.video: thumb_path = await generate_thumbnail(path)
                            await client.send_file(dest_id, path or message.text, caption=message.text, thumb=thumb_path, link_preview=False)
                            count += 1
                        except Exception as e:
                            LOGGER.error(f"Clone message error: {e}")
                            errors += 1
                        finally:
                            if path and os.path.exists(path): os.remove(path)
                            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
                else:
                    await client.forward_messages(dest_id, message.id, source_id)
                    count += 1
                await asyncio.sleep(2)
            except FloodWaitError as e:
                await status_msg.edit_text(f"⏳ Rate limited. Waiting {e.seconds} seconds...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                LOGGER.error(f"Error cloning message {message.id}: {e}")
                errors += 1
    except Exception as e:
        await status_msg.edit_text(f"❌ Critical error: {e}")
        return ConversationHandler.END
    await status_msg.edit_text(f"✅ *Clone complete!*\n\n✅ Successful: {count}\n❌ Failed: {errors}\n⏭️ Skipped: {skipped}", parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

# --- Batch Handler ---
async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📦 *Batch Copy Mode*\n\nSend start and end message links (one per line).\n\nFormat:\n`https://t.me/c/CHANNEL_ID/START_MSG_ID`\n`https://t.me/c/CHANNEL_ID/END_MSG_ID`\n\nOr /cancel to abort.", parse_mode='Markdown')
    return GET_LINKS

def parse_message_link(link: str):
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    return (int("-100" + match.group(1)), int(match.group(2))) if match else (None, None)

async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = update.message.text.split()
    if len(links) != 2: await update.message.reply_text("❌ Please provide exactly two links or /cancel."); return GET_LINKS
    start_channel, start_msg_id = parse_message_link(links[0]); end_channel, end_msg_id = parse_message_link(links[1])
    if not all([start_channel, start_msg_id, end_channel, end_msg_id]) or start_channel != end_channel:
        await update.message.reply_text("❌ Invalid or mismatched links. Try again or /cancel."); return GET_LINKS
    context.user_data['batch_info'] = {'channel_id': start_channel, 'start_id': start_msg_id, 'end_id': end_msg_id}
    await update.message.reply_text("✅ Links validated!\n\n📤 Now send destination chat ID or forward a message from destination.\n\nOr /cancel.")
    return GET_BATCH_DESTINATION

async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.forward_origin: dest_id = update.message.forward_origin.chat.id
    else:
        parsed = parse_chat_ids(update.message.text)
        dest_id = parsed[0] if parsed else None
    if not dest_id: await update.message.reply_text("❌ Invalid destination. Try again or /cancel."); return GET_BATCH_DESTINATION
    info = context.user_data['batch_info']
    status_msg = await update.message.reply_text(f"⏳ Fetching messages from {info['start_id']} to {info['end_id']}...")
    count, errors = 0, 0
    try:
        messages_to_copy = await client.get_messages(info['channel_id'], ids=range(info['start_id'], info['end_id'] + 1))
        existing_messages = [m for m in messages_to_copy if m is not None]
        total_found = len(existing_messages)
        await status_msg.edit_text(f"✅ Found {total_found} messages. Starting copy process...")
        i = 0
        while i < total_found:
            message = existing_messages[i]
            if (i + 1) % 5 == 0: await status_msg.edit_text(f"⏳ Progress: {i+1}/{total_found} messages processed...")
            if message.grouped_id:
                album = [m for m in existing_messages[i:] if m and m.grouped_id == message.grouped_id]
                album_paths, thumb_path = [], None
                try:
                    for j, msg in enumerate(album):
                        path = await msg.download_media(file=f"temp_batch_{msg.id}_{j}")
                        album_paths.append(path)
                        if not thumb_path and msg.video: thumb_path = await generate_thumbnail(path)
                    await client.send_file(dest_id, album_paths, caption=album[0].text, thumb=thumb_path)
                    count += len(album)
                except Exception as e:
                    LOGGER.error(f"Batch album copy error for group {message.grouped_id}: {e}")
                    errors += len(album)
                finally:
                    for path in album_paths:
                        if os.path.exists(path): os.remove(path)
                    if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
                i += len(album)
            else:
                await process_single_message(dest_id, message, message.text)
                count += 1
                i += 1
            await asyncio.sleep(2)
    except Exception as e:
        await status_msg.edit_text(f"❌ Critical error during batch: {e}")
        return ConversationHandler.END
    await status_msg.edit_text(f"✅ *Batch complete!*\n\n✅ Successful: {count}\n❌ Failed: {errors}", parse_mode='Markdown')
    return ConversationHandler.END

# --- Main ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 *Welcome!*\n\n/forward - Manage tasks\n/batch - Batch Copy\n/clone - Clone Channel\n/help - Show help", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 *Help*\n\nUse /forward to create tasks.\nUse `-word` to remove items from lists.\nUse `/clear` to empty a list.", parse_mode='Markdown')

async def main():
    global MY_ID
    application = Application.builder().token(BOT_TOKEN).build()

    cancel_handler = CommandHandler('cancel', cancel)
    
    # --- CRITICAL FIX: allow_reentry=True ---
    # This ensures that if you type /forward while inside a menu, it restarts correctly.
    # We also changed filters.TEXT to allow commands like /clear to pass through to the handler.
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("forward", forward_command_handler),
            CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|settings_menu|settings_toggle_beautify|settings_toggle_filter|settings_toggle_blockme|view_stats)"),
            CallbackQueryHandler(new_task_start, pattern="^new_task_start$")
        ],
        states={
            MAIN_MENU: [
                CommandHandler("forward", forward_command_handler),
                CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
                CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|delete_execute|settings_menu|settings_toggle_beautify|settings_toggle_filter|settings_toggle_blockme|view_stats|back_to_main_menu)")
            ],
            SETTINGS_MENU: [
                CommandHandler("forward", forward_command_handler),
                CallbackQueryHandler(edit_setting_ask, pattern="^settings_edit_"),
                CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$"),
                CallbackQueryHandler(callback_query_handler, pattern="^(settings_toggle_beautify|settings_toggle_filter|settings_toggle_blockme|settings_menu)")
            ],
            ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
            ASK_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_source)],
            ASK_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_destination)],
            
            # FIX: Allowed commands in these handlers so /clear is not filtered out
            ASK_FOOTER: [MessageHandler(filters.TEXT, get_footer), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")],
            ASK_REPLACE: [MessageHandler(filters.TEXT, get_replace_rules), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")],
            ASK_REMOVE: [MessageHandler(filters.TEXT, get_remove_texts), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")],
            ASK_BLACKLIST: [MessageHandler(filters.TEXT, get_blacklist), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")],
            ASK_WHITELIST: [MessageHandler(filters.TEXT, get_whitelist), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")],
            ASK_DELAY: [MessageHandler(filters.TEXT, get_delay), CallbackQueryHandler(callback_query_handler, pattern="^settings_menu:")]
        },
        fallbacks=[cancel_handler],
        per_message=False,
        allow_reentry=True # This fixes the stuck /forward issue
    )

    batch_conv = ConversationHandler(
        entry_points=[CommandHandler('batch', batch_start)], 
        states={
            GET_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_links)], 
            GET_BATCH_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_batch_destination)]
        }, 
        fallbacks=[cancel_handler]
    )
    
    clone_conv = ConversationHandler(
        entry_points=[CommandHandler('clone', clone_start)], 
        states={
            CLONE_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, clone_get_source)], 
            CLONE_DEST: [MessageHandler(filters.ALL & ~filters.COMMAND, clone_get_dest)], 
            CLONE_RESTRICTED: [
                CallbackQueryHandler(clone_set_restricted, pattern="^clone_restricted:"), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, clone_process_skip)
            ]
        }, 
        fallbacks=[cancel_handler]
    )

    application.add_handler(conv_handler)
    application.add_handler(batch_conv)
    application.add_handler(clone_conv)
    application.add_handler(MessageHandler(filters.Regex(r'https?://t\.me/') & filters.TEXT, auto_save_handler))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    LOGGER.info("Bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    await client.start()
    me = await client.get_me()
    MY_ID = me.id
    LOGGER.info(f"Telethon started as: {me.first_name}")
    
    await client.run_until_disconnected()
    await application.stop()

if __name__ == "__main__":
    asyncio.run(main())