import asyncio
import os
import re
import random
import logging
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Message
from telethon.errors.rpcerrorlist import PeerIdInvalidError
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

# --- DATABASE SETUP ---
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client.forwarder_bot
    tasks_collection = db.tasks
    LOGGER.info("Successfully connected to MongoDB.")
except Exception as e:
    LOGGER.error(f"Error connecting to MongoDB: {e}")
    exit(1)

# --- HELPER FUNCTIONS ---
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
    caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}"]
    video_links = [f"V{i}:\n{link}" for i, link in enumerate(links, 1)]
    caption_parts.extend(video_links)
    return "\n\n".join(caption_parts)

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
                    find, repl = rule.split('=>', 1); final_caption = final_caption.replace(find.strip(), repl.strip())
        if mods.get("beautiful_captions"):
            new_caption = create_beautiful_caption(final_caption)
            if new_caption: final_caption = new_caption
        if final_caption:
            final_caption = re.sub(r'\n{3,}', '\n\n', final_caption).strip()
        if mods.get("footer_text"):
            final_caption = f"{final_caption or ''}\n\n{mods['footer_text']}"
        
        # --- FIX #1: Use the direct, reliable copy method ---
        for dest_id in task.get("destination_ids", []):
            LOGGER.info(f"Copying message {message.id} from task '{task['_id']}' to {dest_id}")
            try:
                # This method copies media and applies the new caption, without "Forwarded from"
                await client.send_message(dest_id, file=message.media, message=final_caption)
            except Exception as e:
                LOGGER.error(f"Failed to copy message to {dest_id}: {e}")
            delay = task.get("settings", {}).get("delay", 0)
            if delay > 0: await asyncio.sleep(delay)
        # --- END OF FIX #1 ---

# --- TELEGRAM BOT INTERFACE ---
(ASK_LABEL, ASK_SOURCE, ASK_DESTINATION, ASK_FOOTER, ASK_REPLACE, ASK_REMOVE) = range(6)
(MAIN_MENU, SETTINGS_MENU, GET_LINKS, GET_BATCH_DESTINATION) = range(6, 10)

async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, from_cancel=False):
    user_id = update.effective_user.id
    tasks = list(tasks_collection.find({"owner_id": user_id}))
    buttons = [[InlineKeyboardButton(f"{'✅' if t.get('status') == 'active' else '❌'} {t['_id']}", callback_data=f"toggle_status:{t['_id']}"),
                InlineKeyboardButton("⚙️ Settings", callback_data=f"settings_menu:{t['_id']}"),
                InlineKeyboardButton("🗑️", callback_data=f"delete_confirm:{t['_id']}")] for t in tasks]
    keyboard = InlineKeyboardMarkup([*buttons, [InlineKeyboardButton("➕ Create New Task", callback_data="new_task_start")]])
    text = "Your Forwarding Tasks:" if tasks else "You have no tasks. Create one!"
    if update.callback_query and not from_cancel:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=keyboard)
    return MAIN_MENU

async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /save <message_link>"); return
    link = context.args[0]; match = re.match(r"https?://t\.me/(c/)?(\w+)/(\d+)", link)
    if not match:
        await update.message.reply_text("Invalid message link format."); return
    try:
        is_private = match.group(1); channel_id_str = match.group(2)
        chat_id = int(f"-100{channel_id_str}") if is_private else channel_id_str
        msg_id = int(match.group(3))
        status_msg = await update.message.reply_text("Fetching post...")
        message = await client.get_messages(chat_id, ids=msg_id)
        if not message:
            await status_msg.edit_text("Could not fetch message."); return
        media_content = await message.download_media(file=bytes) if message.media else None
        await status_msg.delete()
        if message.photo: await update.message.reply_photo(photo=media_content, caption=message.text)
        elif message.video: await update.message.reply_video(video=media_content, caption=message.text)
        elif message.document: await update.message.reply_document(document=media_content, caption=message.text)
        elif message.text: await update.message.reply_text(message.text)
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}\n\nMake sure your User Account has joined the source channel.")
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
        await query.edit_message_text(f"Task '{value}' has been deleted."); await asyncio.sleep(2)
        return await forward_command_handler(update, context)
    elif query.data == "back_to_main_menu":
        return await forward_command_handler(update, context)
    elif action == "settings_toggle_beautify":
        task = tasks_collection.find_one({"_id": value, "owner_id": user_id})
        if task:
            current_status = task.get("modifications", {}).get("beautiful_captions", False)
            tasks_collection.update_one({"_id": value}, {"$set": {"modifications.beautiful_captions": not current_status}})
        context.user_data['current_task_id'] = value
        return await show_settings_menu(update, context)
    elif action == "settings_menu":
        context.user_data['current_task_id'] = value
        return await show_settings_menu(update, context)

# --- NEW FEATURE: Function to get chat titles ---
async def get_chat_titles(ids: list) -> str:
    titles = []
    for chat_id in ids:
        try:
            entity = await client.get_entity(chat_id)
            titles.append(f"{entity.title} (`{chat_id}`)")
        except (ValueError, PeerIdInvalidError):
            titles.append(f"Invalid ID (`{chat_id}`)")
        except Exception:
            titles.append(f"Unknown Chat (`{chat_id}`)")
    return "\n".join(titles)

# --- NEW FEATURE: Enhanced Settings Menu ---
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = context.user_data.get('current_task_id')
    task = tasks_collection.find_one({"_id": task_id})
    if not task:
        await update.callback_query.edit_message_text("Error: Task not found.")
        return MAIN_MENU

    beautify_status = task.get("modifications", {}).get("beautiful_captions", False)
    beautify_emoji = "✅" if beautify_status else "❌"
    
    source_info = await get_chat_titles(task.get('source_ids', []))
    dest_info = await get_chat_titles(task.get('destination_ids', []))

    text = (f"*Settings for task: {task_id}*\n\n"
            f"*Source(s):*\n{source_info}\n\n"
            f"*Destination(s):*\n{dest_info}")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Edit Footer", callback_data="settings_edit_footer")],
        [InlineKeyboardButton("🔄 Edit Replace Rules", callback_data="settings_edit_replace")],
        [InlineKeyboardButton("✂️ Edit Remove Texts", callback_data="settings_edit_remove")],
        [InlineKeyboardButton(f"{beautify_emoji} Beautiful Captions", callback_data=f"settings_toggle_beautify:{task_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]])
    await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
    return SETTINGS_MENU

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Please provide a unique name for this task.\n\nOr /cancel.")
    return ASK_LABEL
async def get_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if tasks_collection.find_one({"_id": label, "owner_id": update.effective_user.id}):
        await update.message.reply_text("Label exists. Try another or /cancel."); return ASK_LABEL
    context.user_data['new_task_label'] = label
    await update.message.reply_text("✅ Label set. Send Source Chat ID(s) or forward a message.\n\nOr /cancel.")
    return ASK_SOURCE
async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids:
        await update.message.reply_text("Invalid ID. Send numeric IDs or forward a message. Or /cancel."); return ASK_SOURCE
    context.user_data['new_task_source'] = ids
    await update.message.reply_text("✅ Source(s) set. Send Destination Chat ID(s).\n\nOr /cancel.")
    return ASK_DESTINATION
async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ids = [update.message.forward_origin.chat.id] if update.message.forward_origin else parse_chat_ids(update.message.text)
    if not ids:
        await update.message.reply_text("Invalid ID. Send numeric IDs or forward a message. Or /cancel."); return ASK_DESTINATION
    tasks_collection.insert_one({"_id": context.user_data['new_task_label'], "owner_id": update.effective_user.id, "status": "active",
        "source_ids": context.user_data['new_task_source'], "destination_ids": ids,
        "modifications": {"footer_text": None, "replace_rules": None, "remove_texts": None, "beautiful_captions": False}, 
        "settings": {"delay": 0}})
    context.user_data.clear()
    await update.message.reply_text("✅ Task created successfully!")
    await forward_command_handler(update, context)
    return ConversationHandler.END

async def edit_setting_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.callback_query.data
    state_map = {"settings_edit_footer": (ASK_FOOTER, "Send new footer. /skip to remove."),
                 "settings_edit_replace": (ASK_REPLACE, "Send replace rules (`find => replace`). /skip to remove."),
                 "settings_edit_remove": (ASK_REMOVE, "Send texts to remove (one per line). /skip to remove.")}
    if action in state_map:
        state, text = state_map[action]
        await update.callback_query.edit_message_text(text + "\n\nOr /cancel."); return state
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
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Operation cancelled.")
    await forward_command_handler(update, context, from_cancel=True)
    return ConversationHandler.END

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("**Batch Forwarder**\n\nSend start and end message links.\n\nOr /cancel.", parse_mode='Markdown')
    return GET_LINKS
def parse_message_link(link: str):
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    return (int("-100" + match.group(1)), int(match.group(2))) if match else (None, None)
async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = update.message.text.split()
    if len(links) != 2:
        await update.message.reply_text("Please provide exactly two links or /cancel."); return GET_LINKS
    start_channel, start_msg_id = parse_message_link(links[0])
    end_channel, end_msg_id = parse_message_link(links[1])
    if not all([start_channel, start_msg_id, end_channel, end_msg_id]) or start_channel != end_channel:
        await update.message.reply_text("Invalid or mismatched links. Or /cancel."); return GET_LINKS
    context.user_data['batch_info'] = {'channel_id': start_channel, 'start_id': start_msg_id, 'end_id': end_msg_id}
    await update.message.reply_text("✅ Links OK. Send destination chat ID.\n\nOr /cancel.")
    return GET_BATCH_DESTINATION
async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest_id = update.message.forward_origin.chat.id if update.message.forward_origin else (parse_chat_ids(update.message.text) or [None])[0]
    if not dest_id:
        await update.message.reply_text("Invalid destination. Try again or /cancel."); return GET_BATCH_DESTINATION
    info = context.user_data['batch_info']; total = info['end_id'] - info['start_id'] + 1
    status_msg = await update.message.reply_text(f"Starting batch copy of {total} messages...")
    count, errors = 0, 0
    try:
        msg_ids = range(info['start_id'], info['end_id'] + 1)
        for i, msg_id in enumerate(msg_ids):
            message = await client.get_messages(info['channel_id'], ids=msg_id)
            if message:
                # Use the direct copy method here as well
                try:
                    await client.send_message(dest_id, message)
                    count += 1
                except Exception:
                    errors += 1
            else: errors += 1
            if (i + 1) % 10 == 0: await status_msg.edit_text(f"Progress: {i+1}/{total} messages processed...")
            await asyncio.sleep(1.5)
    except Exception as e:
        await status_msg.edit_text(f"A critical error occurred: {e}"); return ConversationHandler.END
    await status_msg.edit_text(f"✅ Batch complete!\n\nSuccess: {count}\nFailed: {errors}")
    return ConversationHandler.END

async def main():
    global MY_ID
    application = Application.builder().token(BOT_TOKEN).build()
    cancel_handler = CommandHandler('cancel', cancel)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("forward", forward_command_handler), 
                      CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|settings_menu|settings_toggle_beautify)"),
                      CallbackQueryHandler(new_task_start, pattern="^new_task_start$")],
        states={
            MAIN_MENU: [CallbackQueryHandler(new_task_start, pattern="^new_task_start$"),
                        CallbackQueryHandler(callback_query_handler, pattern="^(toggle_status|delete_confirm|settings_menu|settings_toggle_beautify)")],
            SETTINGS_MENU: [CallbackQueryHandler(edit_setting_ask, pattern="^settings_edit_"),
                            CallbackQueryHandler(forward_command_handler, pattern="^back_to_main_menu$"),
                            CallbackQueryHandler(callback_query_handler, pattern="^settings_toggle_beautify")],
            ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_label)],
            ASK_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_source)],
            ASK_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_destination)],
            ASK_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_footer)],
            ASK_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_replace_rules)],
            ASK_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_remove_texts)],
        }, fallbacks=[cancel_handler], per_message=False )
    batch_conv = ConversationHandler(
        entry_points=[CommandHandler('batch', batch_start)],
        states={ GET_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_links)],
                 GET_BATCH_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_batch_destination)],
        }, fallbacks=[cancel_handler])
    application.add_handler(conv_handler); application.add_handler(batch_conv)
    application.add_handler(CommandHandler("save", save_command)); application.add_handler(CommandHandler("start", forward_command_handler))
    LOGGER.info("Control Bot starting..."); await application.initialize(); await application.start(); await application.updater.start_polling(); LOGGER.info("Control Bot started.")
    await client.start()
    me = await client.get_me(); MY_ID = me.id; LOGGER.info(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    LOGGER.info("Warming up Telethon client and fetching dialogs...");
    try:
        async for _ in client.iter_dialogs(): pass
        LOGGER.info("Dialogs fetched successfully.")
    except Exception as e:
        LOGGER.warning(f"Could not pre-fetch dialogs: {e}")
    await client.run_until_disconnected(); await application.updater.stop(); await application.stop()
if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): LOGGER.info("Bot stopped gracefully.")