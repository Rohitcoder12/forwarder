import asyncio
import os
import re
import random
import cv2
import logging
from PIL import Image
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Message
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)
from pymongo import MongoClient

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
LOGGER = logging.getLogger(__name__)

# --- CONFIGURATION & STATE ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

SESSION_NAME = "telegram_forwarder"
MY_ID = None

# --- VALIDATE CONFIG ---
if not all([API_ID, API_HASH, BOT_TOKEN, MONGO_URI]):
    raise RuntimeError("API credentials and MONGO_URI must be set in .env file.")

# --- DATABASE SETUP (MONGODB) ---
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client.forwarder_bot
    tasks_collection = db.tasks
    LOGGER.info("Successfully connected to MongoDB.")
except Exception as e:
    LOGGER.error(f"Error connecting to MongoDB: {e}")
    exit(1)

# --- HELPER & CORE LOGIC FUNCTIONS ---
def parse_chat_ids(text: str) -> list[int] | None:
    try:
        return [int(i.strip()) for i in text.split(',')]
    except (ValueError, TypeError):
        return None

async def resend_message(destination_id: int, message: Message, caption: str | None):
    dl_path, thumb_path = None, None
    try:
        if message.media:
            dl_path = await message.download_media(file=bytes)
            temp_file_for_thumb = None
            if message.video:
                temp_file_for_thumb = "temp_video_for_thumb.tmp"
                with open(temp_file_for_thumb, "wb") as f:
                    f.write(dl_path)
                thumb_path = await generate_thumbnail(temp_file_for_thumb)
                if os.path.exists(temp_file_for_thumb):
                    os.remove(temp_file_for_thumb)

            await client.send_file(destination_id, dl_path, caption=caption, thumb=thumb_path)
        elif message.text:
            await client.send_message(destination_id, caption)
        return True
    except Exception as e:
        LOGGER.error(f"Failed to resend message to {destination_id}: {e}")
        return False
    finally:
        if isinstance(dl_path, str) and os.path.exists(dl_path): os.remove(dl_path)
        if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

async def generate_thumbnail(video_path):
    try:
        thumb_path = os.path.splitext(video_path)[0] + ".jpg"
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened(): return None
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
        LOGGER.error(f"Thumbnail generation failed: {e}")
        return None

# --- TELETHON CLIENT ENGINE ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID: return
    message = event.message
    active_tasks = tasks_collection.find({"source_ids": event.chat_id, "status": "active"})
    for task in active_tasks:
        mods = task.get("modifications", {})
        final_caption = message.text
        if mods.get("remove_texts") and final_caption:
            lines_to_remove = {line.strip() for line in mods["remove_texts"].splitlines() if line.strip()}
            kept_lines = [line for line in final_caption.splitlines() if line.strip() not in lines_to_remove]
            final_caption = "\n".join(kept_lines)
        if mods.get("replace_rules") and final_caption:
            for rule in mods["replace_rules"].splitlines():
                if '=>' in rule:
                    find, repl = rule.split('=>', 1)
                    final_caption = final_caption.replace(find.strip(), repl.strip())
        if final_caption:
            final_caption = re.sub(r'\n{3,}', '\n\n', final_caption).strip()
        if mods.get("footer_text"):
            final_caption = f"{final_caption or ''}\n\n{mods['footer_text']}"
        for dest_id in task.get("destination_ids", []):
            LOGGER.info(f"Forwarding message {message.id} from task '{task['_id']}' to {dest_id}")
            await resend_message(dest_id, message, final_caption)
            delay = task.get("settings", {}).get("delay", 0)
            if delay > 0:
                await asyncio.sleep(delay)

# --- TELEGRAM BOT INTERFACE (python-telegram-bot v20+) ---
(ASK_LABEL, ASK_SOURCE, ASK_DESTINATION, ASK_FOOTER, ASK_REPLACE, ASK_REMOVE) = range(6)
(MAIN_MENU, SETTINGS_MENU, GET_LINKS, GET_BATCH_DESTINATION) = range(6, 10)

# --- NEW: ROBUST CONVERSATION HELPERS ---
async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles unexpected messages during a conversation."""
    await update.message.reply_text("I'm waiting for a specific input. Please provide it or use /cancel.")

async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = list(tasks_collection.find({"owner_id": user_id}))
    buttons = [[InlineKeyboardButton(f"{'✅' if t.get('status') == 'active' else '❌'} {t['_id']}", callback_data=f"toggle_status:{t['_id']}"),
                InlineKeyboardButton("⚙️ Settings", callback_data=f"settings_menu:{t['_id']}"),
                InlineKeyboardButton("🗑️", callback_data=f"delete_confirm:{t['_id']}")] for t in tasks]
    keyboard = InlineKeyboardMarkup([*buttons, [InlineKeyboardButton("➕ Create New Task", callback_data="new_task_start")]])
    text = "Your Forwarding Tasks:" if tasks else "You have no tasks. Create one!"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)
    return MAIN_MENU

# --- FIX: REWRITTEN /save COMMAND ---
async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /save <message_link>")
        return

    link = context.args[0]
    match = re.match(r"https?://t\.me/(c/)?(\w+)/(\d+)", link)
    if not match:
        await update.message.reply_text("Invalid message link format.")
        return

    try:
        chat_id = f"-100{match.group(2)}" if match.group(1) else match.group(2)
        msg_id = int(match.group(3))
        status_msg = await update.message.reply_text("Fetching post...")
        message = await client.get_messages(chat_id, ids=msg_id)
        
        if not message:
            await status_msg.edit_text("Could not fetch the message. Make sure the link is correct and I have access.")
            return

        # Download media to memory (bytes)
        media_content = await message.download_media(file=bytes) if message.media else None
        
        await status_msg.delete()

        # Reply using the bot, not the user client
        if message.photo:
            await update.message.reply_photo(photo=media_content, caption=message.text)
        elif message.video:
            await update.message.reply_video(video=media_content, caption=message.text)
        elif message.document:
            await update.message.reply_document(document=media_content, caption=message.text)
        elif message.text:
            await update.message.reply_text(message.text)
            
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}\n\nMake sure your User Account (not the bot) has joined the source channel/group.")
        LOGGER.error(f"Error in /save command: {e}")

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
        return await forward_command_handler(update, context)
    elif action == "delete_confirm":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Delete It", callback_data=f"delete_execute:{value}")], [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main_menu")]])
        await query.edit_message_text(f"Are you sure you want to delete task '{value}'?", reply_markup=keyboard)
        return MAIN_MENU
    elif action == "delete_execute":
        tasks_collection.delete_one({"_id": value, "owner_id": user_id})
        await query.edit_message_text(f"Task '{value}' has been deleted.")
        await asyncio.sleep(2)
        return await forward_command_handler(update, context)
    elif query.data == "back_to_main_menu":
        return await forward_command_handler(update, context)
    elif action == "settings_menu":
        context.user_data['current_task_id'] = value
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Edit Footer", callback_data="settings_edit_footer")],
            [InlineKeyboardButton("🔄 Edit Replace Rules", callback_data="settings_edit_replace")],
            [InlineKeyboardButton("✂️ Edit Remove Texts", callback_data="settings_edit_remove")],
            [InlineKeyboardButton("⬅️ Back to Task List", callback_data="back_to_main_menu")]
        ])
        await query.edit_message_text(f"Settings for task: *{value}*", reply_markup=keyboard, parse_mode='Markdown')
        return SETTINGS_MENU

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Please provide a unique name (label) for this task (e.g., `ChannelA_to_B`).\n\nOr /cancel to go back.")
    return ASK_LABEL
async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if tasks_collection.find_one({"_id": label, "owner_id": update.effective_user.id}):
        await update.message.reply_text("A task with this label already exists. Please choose another or /cancel.")
        return ASK_LABEL
    context.user_data['new_task_label'] = label
    await update.message.reply_text("✅ Label set. Now, send the Source Chat ID(s), separated by commas, or forward a message from the source channel.\n\nOr /cancel to go back.")
    return ASK_SOURCE
async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids:
        await update.message.reply_text("Invalid ID format. Please send numeric IDs or forward a message. Or /cancel.")
        return ASK_SOURCE
    context.user_data['new_task_source'] = ids
    await update.message.reply_text("✅ Source(s) set. Now, send the Destination Chat ID(s).\n\nOr /cancel to go back.")
    return ASK_DESTINATION
async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids:
        await update.message.reply_text("Invalid ID format. Please send numeric IDs or forward a message. Or /cancel.")
        return ASK_DESTINATION
    tasks_collection.insert_one({
        "_id": context.user_data['new_task_label'], "owner_id": update.effective_user.id, "status": "active",
        "source_ids": context.user_data['new_task_source'], "destination_ids": ids,
        "modifications": { "footer_text": None, "replace_rules": None, "remove_texts": None}, "settings": {"delay": 0}
    })
    context.user_data.clear()
    await update.message.reply_text("✅ Task created successfully!")
    await forward_command_handler(update, context)
    return ConversationHandler.END

async def edit_setting_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.callback_query.data
    state_map = {"settings_edit_footer": (ASK_FOOTER, "Send the new footer text. Send /skip to remove."),
                 "settings_edit_replace": (ASK_REPLACE, "Send replace rules in `find => replace` format. Send /skip to remove."),
                 "settings_edit_remove": (ASK_REMOVE, "Send texts to remove, one per line. Send /skip to remove.")}
    if action in state_map:
        state, text = state_map[action]
        await update.callback_query.edit_message_text(text + "\n\nOr /cancel to go back.")
        return state
    return SETTINGS_MENU
async def save_setting_text(update: Update, context: ContextTypes.DEFAULT_TYPE, field_key: str):
    task_id = context.user_data.get('current_task_id')
    if not task_id: return ConversationHandler.END
    new_value = update.message.text if update.message.text.lower() != '/skip' else None
    tasks_collection.update_one({"_id": task_id}, {"$set": {f"modifications.{field_key}": new_value}})
    await update.message.reply_text("✅ Setting updated!")
    await forward_command_handler(update, context)
    return ConversationHandler.END
async def get_footer(update: Update, context: ContextTypes.DEFAULT_TYPE): return await save_setting_text(update, context, "footer_text")
async def get_replace_rules(update: Update, context: ContextTypes.DEFAULT_TYPE): return await save_setting_text(update, context, "replace_rules")
async def get_remove_texts(update: Update, context: ContextTypes.DEFAULT_TYPE): return await save_setting_text(update, context, "remove_texts")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    await forward_command_handler(update, context)
    return ConversationHandler.END

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("**Batch Forwarder**\n\nSend the start and end message links, separated by a space.\n\nOr /cancel to go back.", parse_mode='Markdown')
    return GET_LINKS
def parse_message_link(link: str):
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    return (int("-100" + match.group(1)), int(match.group(2))) if match else (None, None)
async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = update.message.text.split()
    if len(links) != 2:
        await update.message.reply_text("Please provide exactly two links or /cancel.")
        return GET_LINKS
    start_channel, start_msg_id = parse_message_link(links[0])
    end_channel, end_msg_id = parse_message_link(links[1])
    if not all([start_channel, start_msg_id, end_channel, end_msg_id]) or start_channel != end_channel:
        await update.message.reply_text("Invalid or mismatched links. Both must be from the same private channel. Or /cancel.")
        return GET_LINKS
    context.user_data['batch_info'] = {'channel_id': start_channel, 'start_id': start_msg_id, 'end_id': end_msg_id}
    await update.message.reply_text("✅ Links OK. Now, send the destination chat ID or forward a message from there.\n\nOr /cancel to go back.")
    return GET_BATCH_DESTINATION
async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest_id = update.message.forward_origin.chat.id if update.message.forward_origin else (parse_chat_ids(update.message.text) or [None])[0]
    if not dest_id:
        await update.message.reply_text("Invalid destination. Please try again or /cancel.")
        return GET_BATCH_DESTINATION
    info = context.user_data['batch_info']
    total = info['end_id'] - info['start_id'] + 1
    status_msg = await update.message.reply_text(f"Starting batch forward of {total} messages...")
    count, errors = 0, 0
    try:
        msg_ids = range(info['start_id'], info['end_id'] + 1)
        for i, msg_id in enumerate(msg_ids):
            message = await client.get_messages(info['channel_id'], ids=msg_id)
            if message and await resend_message(dest_id, message, message.text): count += 1
            else: errors += 1
            if (i + 1) % 10 == 0: await status_msg.edit_text(f"Progress: {i+1}/{total} messages processed...")
            await asyncio.sleep(1.5)
    except Exception as e:
        await status_msg.edit_text(f"A critical error occurred: {e}")
        return ConversationHandler.END
    await status_msg.edit_text(f"✅ Batch complete!\n\nSuccessfully forwarded: {count}\nFailed: {errors}")
    return ConversationHandler.END

async def main():
    global MY_ID
    application = Application.builder().token(BOT_TOKEN).build()
    
    cancel_handler = CommandHandler('cancel', cancel)
    unexpected_handler = MessageHandler(filters.ALL, unexpected_message)
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("forward", forward_command_handler), CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
                      CallbackQueryHandler(callback_query_handler, pattern="^(toggle|delete|back|settings)")],
        states={
            MAIN_MENU: [CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
                        CallbackQueryHandler(callback_query_handler, pattern="^(toggle|delete|settings)")],
            SETTINGS_MENU: [CallbackQueryHandler(edit_setting_ask, pattern="^settings_edit_"),
                            CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$")],
            ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
            ASK_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_source)],
            ASK_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_destination)],
            ASK_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_footer)],
            ASK_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_replace_rules)],
            ASK_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_remove_texts)],
        },
        fallbacks=[cancel_handler, CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$")],
        per_message=False
    )
    
    batch_conv = ConversationHandler(
        entry_points=[CommandHandler('batch', batch_start)],
        states={
            GET_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_links), unexpected_handler],
            GET_BATCH_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_batch_destination), unexpected_handler],
        },
        fallbacks=[cancel_handler]
    )
    application.add_handler(conv_handler)
    application.add_handler(batch_conv)
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("start", forward_command_handler))
    
    LOGGER.info("Control Bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    LOGGER.info("Control Bot started.")

    await client.start()
    me = await client.get_me()
    MY_ID = me.id
    LOGGER.info(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    await client.run_until_disconnected()
    await application.updater.stop()
    await application.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("Bot stopped gracefully.")