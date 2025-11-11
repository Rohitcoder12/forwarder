import asyncio
import os
import re
import random
import cv2
import logging
from PIL import Image
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Message  # <-- FIX APPLIED HERE
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
    """Parses a comma-separated string of chat IDs into a list of integers."""
    try:
        return [int(i.strip()) for i in text.split(',')]
    except (ValueError, TypeError):
        return None

async def resend_message(destination_id: int, message: Message, caption: str | None): # <-- FIX APPLIED HERE
    """
    Downloads and resends a message to bypass restrictions.
    Returns True on success, False on failure.
    """
    dl_path, thumb_path = None, None
    try:
        if message.media:
            # Using file=bytes can be more robust for environments without persistent storage
            dl_path = await message.download_media(file=bytes)
            # Create a temporary file if thumbnail generation is needed
            temp_file_for_thumb = None
            if message.video:
                if isinstance(dl_path, bytes):
                    temp_file_for_thumb = "temp_video_for_thumb"
                    with open(temp_file_for_thumb, "wb") as f:
                        f.write(dl_path)
                    thumb_path = await generate_thumbnail(temp_file_for_thumb)
                else: # if dl_path is a string path
                    thumb_path = await generate_thumbnail(dl_path)

            await client.send_file(destination_id, dl_path, caption=caption, thumb=thumb_path)
            
            if temp_file_for_thumb and os.path.exists(temp_file_for_thumb):
                os.remove(temp_file_for_thumb)

        elif message.text:
            await client.send_message(destination_id, caption)
        return True
    except Exception as e:
        LOGGER.error(f"Failed to resend message to {destination_id}: {e}")
        return False
    finally:
        # Cleanup logic
        if dl_path and isinstance(dl_path, str) and os.path.exists(dl_path): os.remove(dl_path)
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
MAIN_MENU, SETTINGS_MENU = range(7, 9)

async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = list(tasks_collection.find({"owner_id": user_id}))
    
    buttons = []
    for task in tasks:
        status_emoji = "✅" if task.get('status') == 'active' else "❌"
        buttons.append([
            InlineKeyboardButton(f"{status_emoji} {task['_id']}", callback_data=f"toggle_status:{task['_id']}"),
            InlineKeyboardButton("⚙️ Settings", callback_data=f"settings_menu:{task['_id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"delete_confirm:{task['_id']}"),
        ])

    keyboard = InlineKeyboardMarkup([
        *buttons,
        [InlineKeyboardButton("➕ Create New Task", callback_data="new_task_start")],
    ])
    
    message_text = "Your Forwarding Tasks:" if tasks else "You have no tasks. Create one!"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=keyboard)
    else:
        await update.message.reply_text(message_text, reply_markup=keyboard)
    return MAIN_MENU

async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /save <message_link>")
        return

    link = context.args[0]
    match = re.match(r"https?://t\.me/(c/)?(\d+|[a-zA-Z0-9_]+)/(\d+)", link)
    if not match:
        await update.message.reply_text("Invalid message link format.")
        return

    is_private = match.group(1)
    channel_part = match.group(2)
    msg_id = int(match.group(3))

    try:
        if is_private:
            chat_id = int("-100" + channel_part)
        else:
            chat_id = channel_part
    except ValueError:
        await update.message.reply_text("Invalid channel ID in link.")
        return

    try:
        await update.message.reply_text("Fetching post...")
        message = await client.get_messages(chat_id, ids=msg_id)
        if not message:
            await update.message.reply_text("Could not fetch message. Ensure link is correct & account has access.")
            return

        success = await resend_message(update.effective_chat.id, message, message.text)
        if not success:
             await update.message.reply_text("Failed to save the post.")
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete It", callback_data=f"delete_execute:{value}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main_menu")]
        ])
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
    query = update.callback_query
    await query.edit_message_text("Let's create a new task.\n\nPlease provide a unique name (label) for this task (e.g., `ChannelA_to_B`).")
    return ASK_LABEL

async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if tasks_collection.find_one({"_id": label, "owner_id": update.effective_user.id}):
        await update.message.reply_text("A task with this label already exists. Please choose another.")
        return ASK_LABEL
    context.user_data['new_task_label'] = label
    await update.message.reply_text("✅ Label set. Now, send the Source Chat ID(s), separated by commas.\nYou can also forward a message from the source channel.")
    return ASK_SOURCE
    
async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = []
    if update.message.forward_origin:
        ids = [update.message.forward_origin.chat.id]
    else:
        ids = parse_chat_ids(update.message.text)
    
    if not ids:
        await update.message.reply_text("Invalid ID format. Please send numeric IDs or forward a message.")
        return ASK_SOURCE
    
    context.user_data['new_task_source'] = ids
    await update.message.reply_text("✅ Source(s) set. Now, send the Destination Chat ID(s).")
    return ASK_DESTINATION

async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = []
    if update.message.forward_origin:
        ids = [update.message.forward_origin.chat.id]
    else:
        ids = parse_chat_ids(update.message.text)
    
    if not ids:
        await update.message.reply_text("Invalid ID format. Please send numeric IDs or forward a message.")
        return ASK_DESTINATION
    
    user_id = update.effective_user.id
    new_task = {
        "_id": context.user_data['new_task_label'], "owner_id": user_id, "status": "active",
        "source_ids": context.user_data['new_task_source'], "destination_ids": ids,
        "modifications": { "footer_text": None, "replace_rules": None, "remove_texts": None},
        "settings": {"delay": 0}
    }
    tasks_collection.insert_one(new_task)
    context.user_data.clear()
    
    await update.message.reply_text("✅ Task created successfully!")
    await forward_command_handler(update, context)
    return ConversationHandler.END

async def edit_setting_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    action = query.data
    
    state_map = {
        "settings_edit_footer": (ASK_FOOTER, "Send the new footer text. Send /skip to remove."),
        "settings_edit_replace": (ASK_REPLACE, "Send replace rules in `find => replace` format, one per line. Send /skip to remove."),
        "settings_edit_remove": (ASK_REMOVE, "Send texts to remove, one phrase per line. Send /skip to remove.")
    }
    
    if action in state_map:
        state, text = state_map[action]
        await query.edit_message_text(text)
        return state
    return SETTINGS_MENU

async def save_setting_text(update: Update, context: ContextTypes.DEFAULT_TYPE, field_key: str):
    task_id = context.user_data.get('current_task_id')
    if not task_id: return ConversationHandler.END

    new_value = update.message.text if update.message.text.lower() != '/skip' else None
    tasks_collection.update_one(
        {"_id": task_id, "owner_id": update.effective_user.id},
        {"$set": {f"modifications.{field_key}": new_value}}
    )
    
    await update.message.reply_text("✅ Setting updated!")
    await forward_command_handler(update, context)
    return ConversationHandler.END

async def get_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await save_setting_text(update, context, "footer_text")
async def get_replace_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await save_setting_text(update, context, "replace_rules")
async def get_remove_texts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await save_setting_text(update, context, "remove_texts")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    await forward_command_handler(update, context)
    return ConversationHandler.END

(GET_LINKS, GET_BATCH_DESTINATION) = range(10, 12)

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "**Batch Forwarder**\n\nSend the start and end message links, separated by a space.",
        parse_mode='Markdown'
    )
    return GET_LINKS

def parse_message_link(link: str):
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    return (int("-100" + match.group(1)), int(match.group(2))) if match else (None, None)

async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = update.message.text.split()
    if len(links) != 2:
        await update.message.reply_text("Please provide exactly two links.")
        return GET_LINKS
    
    start_channel, start_msg_id = parse_message_link(links[0])
    end_channel, end_msg_id = parse_message_link(links[1])

    if not all([start_channel, start_msg_id, end_channel, end_msg_id]) or start_channel != end_channel:
        await update.message.reply_text("Invalid or mismatched links. Both must be from the same private channel.")
        return GET_LINKS
        
    context.user_data['batch_info'] = {'channel_id': start_channel, 'start_id': start_msg_id, 'end_id': end_msg_id}
    await update.message.reply_text("✅ Links OK. Now, send the destination chat ID or forward a message from there.")
    return GET_BATCH_DESTINATION

async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest_id = None
    if update.message.forward_origin:
        dest_id = update.message.forward_origin.chat.id
    else:
        ids = parse_chat_ids(update.message.text)
        if ids: dest_id = ids[0]
    
    if not dest_id:
        await update.message.reply_text("Invalid destination. Please try again.")
        return GET_BATCH_DESTINATION

    info = context.user_data['batch_info']
    total = info['end_id'] - info['start_id'] + 1
    status_msg = await update.message.reply_text(f"Starting batch forward of {total} messages...")
    
    count, errors = 0, 0
    try:
        msg_ids = range(info['start_id'], info['end_id'] + 1)
        for i, msg_id in enumerate(msg_ids):
            message = await client.get_messages(info['channel_id'], ids=msg_id)
            if message and await resend_message(dest_id, message, message.text):
                count += 1
            else:
                errors += 1
            
            if (i + 1) % 10 == 0:
                await status_msg.edit_text(f"Progress: {i+1}/{total} messages processed...")
            await asyncio.sleep(1.5)
    except Exception as e:
        await update.message.reply_text(f"A critical error occurred: {e}")
        return ConversationHandler.END

    await status_msg.edit_text(f"✅ Batch complete!\n\nSuccessfully forwarded: {count}\nFailed: {errors}")
    return ConversationHandler.END

async def main():
    global MY_ID
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("forward", forward_command_handler),
            CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
            CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|delete_execute|back_to_main_menu|settings_menu)")
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
                CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|delete_execute|settings_menu)"),
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(edit_setting_ask, pattern="^settings_edit_"),
                CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$"),
            ],
            ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
            ASK_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_source)],
            ASK_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_destination)],
            ASK_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_footer)],
            ASK_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_replace_rules)],
            ASK_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_remove_texts)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$")],
        per_message=False
    )
    
    batch_conv = ConversationHandler(
        entry_points=[CommandHandler('batch', batch_start)],
        states={
            GET_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_links)],
            GET_BATCH_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_batch_destination)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
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