import asyncio
import os
import re
import random
import cv2
from PIL import Image
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove)
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
from bson.objectid import ObjectId

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
    print("Successfully connected to MongoDB.")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    exit(1)


# --- HELPER FUNCTIONS ---
# (Thumbnail generation and beautiful caption functions are good, let's keep them)
async def generate_thumbnail(video_path):
    try:
        thumb_path = os.path.splitext(video_path)[0] + ".jpg"
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
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

def create_beautiful_caption(original_text):
    link_pattern = r"https?://(?:tera[a-z]+|tinyurl|teraboxurl)\.com/\S+"
    links = re.findall(link_pattern, original_text or "")
    if not links:
        return None
    emojis = random.sample(["😍", "🔥", "❤️", "😈", "💯", "💦", "🔞"], 2)
    caption_parts = [f"Watch Full Videos {emojis[0]}{emojis[1]}"]
    video_links = [f"V{i}:\n{link}" for i, link in enumerate(links, 1)]
    caption_parts.extend(video_links)
    return "\n\n".join(caption_parts)


# --- TELETHON CLIENT ENGINE ---
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

@client.on(events.NewMessage())
async def handle_new_message(event):
    if not MY_ID:
        return
        
    message = event.message
    # Find tasks where this chat is a source
    active_tasks = tasks_collection.find({"source_ids": event.chat_id, "status": "active"})

    for task in active_tasks:
        # --- FILTERS ---
        filters = task.get("filters", {})
        if (filters.get("block_my_messages") and message.sender_id == MY_ID) or \
           (filters.get("block_photos") and message.photo) or \
           (filters.get("block_videos") and message.video) or \
           (filters.get("block_documents") and message.document and not message.video) or \
           (filters.get("block_text") and message.text and not message.media):
            continue

        if filters.get("block_replies_to_me") and message.is_reply:
            try:
                r_msg = await message.get_reply_message()
                if r_msg and r_msg.sender_id == MY_ID:
                    continue
            except Exception:
                pass
        
        # --- TEXT MODIFICATION ---
        mods = task.get("modifications", {})
        final_caption = message.text
        
        # 1. Apply Remove Rules
        if mods.get("remove_texts") and final_caption:
            lines_to_remove = {line.strip() for line in mods["remove_texts"].splitlines() if line.strip()}
            original_lines = final_caption.splitlines()
            kept_lines = [line for line in original_lines if line.strip() not in lines_to_remove]
            final_caption = "\n".join(kept_lines)

        # 2. Apply Replace Rules
        if mods.get("replace_rules") and final_caption:
            for rule in mods["replace_rules"].splitlines():
                if '=>' in rule:
                    find, repl = rule.split('=>', 1)
                    final_caption = final_caption.replace(find.strip(), repl.strip())
        
        # 3. Apply Beautiful Captioning
        if mods.get("beautiful_captions"):
            new_caption = create_beautiful_caption(final_caption)
            if new_caption:
                final_caption = new_caption
        
        # 4. Cleanup and Footer
        if final_caption:
            final_caption = re.sub(r'\n{3,}', '\n\n', final_caption).strip()
        
        if mods.get("footer_text"):
            final_caption = f"{final_caption or ''}\n\n{mods['footer_text']}"

        # --- FORWARDING ---
        for dest_id in task.get("destination_ids", []):
            print(f"Forwarding message {message.id} from task '{task['_id']}' to {dest_id}")
            dl_path, thumb_path = None, None
            try:
                if message.media:
                    dl_path = await message.download_media()
                    if message.video:
                        thumb_path = await generate_thumbnail(dl_path)
                    await client.send_file(dest_id, dl_path, caption=final_caption, thumb=thumb_path)
                elif message.text:
                    await client.send_message(dest_id, final_caption)
                
                # Handle delay if set
                delay = task.get("settings", {}).get("delay", 0)
                if delay > 0:
                    await asyncio.sleep(delay)

            except Exception as e:
                print(f"Failed to send to {dest_id}: {e}")
            finally:
                if dl_path and os.path.exists(dl_path): os.remove(dl_path)
                if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)


# --- TELEGRAM BOT INTERFACE (python-telegram-bot v20+) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the Forwarder Bot! Use /forward to manage your tasks.")

async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main command to show the task dashboard."""
    user_id = update.effective_user.id
    tasks = tasks_collection.find({"owner_id": user_id})
    
    buttons = []
    for task in tasks:
        status_emoji = "✅" if task.get('status') == 'active' else "❌"
        label = task['_id']
        # Each row has a task button and a delete button
        buttons.append([
            InlineKeyboardButton(f"{status_emoji} {label}", callback_data=f"toggle_status:{label}"),
            InlineKeyboardButton("⚙️", callback_data=f"settings:{label}"),
            InlineKeyboardButton("🗑️", callback_data=f"delete_confirm:{label}"),
        ])

    keyboard = InlineKeyboardMarkup([
        *buttons,
        [InlineKeyboardButton("➕ Create New Task", callback_data="new_task")],
        [InlineKeyboardButton("🗑️ Delete All Tasks", callback_data="delete_all_confirm")]
    ])
    
    await update.message.reply_text("Your Forwarding Tasks:", reply_markup=keyboard)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id
    action, _, value = data.partition(':')

    if action == "toggle_status":
        task = tasks_collection.find_one({"_id": value, "owner_id": user_id})
        if task:
            new_status = "stopped" if task.get('status') == 'active' else 'active'
            tasks_collection.update_one({"_id": value}, {"$set": {"status": new_status}})
            await query.message.reply_text(f"Task '{value}' is now {new_status}.")
            # Refresh the dashboard
            await forward_command_handler(query, context)

    elif action == "delete_confirm":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, Delete It", callback_data=f"delete_execute:{value}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_delete")]
        ])
        await query.edit_message_text(f"Are you sure you want to delete task '{value}'?", reply_markup=keyboard)

    elif action == "delete_execute":
        tasks_collection.delete_one({"_id": value, "owner_id": user_id})
        await query.edit_message_text(f"Task '{value}' has been deleted.")
        await forward_command_handler(query, context) # Refresh dashboard

    elif action == "cancel_delete":
        await forward_command_handler(query, context)

    # ... Other actions for settings, new_task etc. will be added ...
    # For now, let's keep it simple. You can expand this.


# --- NEW FEATURE: BATCH FORWARDER ---
(GET_LINKS, GET_DESTINATION) = range(2)

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "**Batch Forwarder Started**\n\n"
        "Please send the starting message link and the ending message link, separated by a space.",
        parse_mode='Markdown'
    )
    return GET_LINKS

def parse_message_link(link: str):
    """Parses a t.me/c/channel_id/message_id link."""
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if match:
        return int("-100" + match.group(1)), int(match.group(2))
    return None, None

async def get_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = update.message.text.split()
    if len(links) != 2:
        await update.message.reply_text("Please provide exactly two links. Start link and end link.")
        return GET_LINKS
    
    start_link, end_link = links[0], links[1]
    
    start_channel, start_msg_id = parse_message_link(start_link)
    end_channel, end_msg_id = parse_message_link(end_link)

    if not all([start_channel, start_msg_id, end_channel, end_msg_id]):
        await update.message.reply_text("Invalid message links. Please provide valid links from a private channel/supergroup.")
        return GET_LINKS

    if start_channel != end_channel:
        await update.message.reply_text("Start and end links must be from the same channel.")
        return GET_LINKS
        
    context.user_data['batch_info'] = {
        'channel_id': start_channel,
        'start_msg_id': start_msg_id,
        'end_msg_id': end_msg_id
    }
    
    await update.message.reply_text("✅ Links received. Now, send the destination chat ID or forward a message from the destination.")
    return GET_DESTINATION

async def get_batch_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest_id = None
    if update.message.forward_from_chat:
        dest_id = update.message.forward_from_chat.id
    else:
        try:
            dest_id = int(update.message.text)
        except ValueError:
            await update.message.reply_text("Invalid destination ID. Please send a valid chat ID or forward a message.")
            return GET_DESTINATION
    
    batch_info = context.user_data['batch_info']
    channel_id = batch_info['channel_id']
    start_id = batch_info['start_msg_id']
    end_id = batch_info['end_msg_id']

    await update.message.reply_text("Starting batch forward... This may take some time.")
    
    count = 0
    try:
        # We iterate from start_id up to end_id.
        async for message in client.iter_messages(channel_id, min_id=start_id - 1, max_id=end_id + 1, reverse=False):
            try:
                await message.forward_to(dest_id)
                count += 1
                await asyncio.sleep(1.5) # IMPORTANT: Avoid flood waits
            except Exception as e:
                print(f"Could not forward message {message.id}: {e}")
                await update.message.reply_text(f"⚠️ Error forwarding message {message.id}. Skipping. Error: {e}")
                await asyncio.sleep(3) # Longer sleep on error
    except Exception as e:
        await update.message.reply_text(f"An error occurred during the batch process: {e}")
        return ConversationHandler.END

    await update.message.reply_text(f"✅ Batch forwarding complete! {count} messages forwarded.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --- MAIN EXECUTION BLOCK ---
async def main():
    global MY_ID
    
    # Start the control bot
    # Note: python-telegram-bot v20 uses a new ApplicationBuilder syntax
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers for the UI
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("forward", forward_command_handler))
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Conversation handler for /batch
    batch_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('batch', batch_start)],
        states={
            GET_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_links)],
            GET_DESTINATION: [MessageHandler(filters.ALL & ~filters.COMMAND, get_batch_destination)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(batch_conv_handler)
    
    print("Control Bot starting...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("Control Bot started.")

    # Start the Telethon client
    await client.start()
    me = await client.get_me()
    MY_ID = me.id
    print(f"Telethon client started as: {me.first_name} (ID: {MY_ID})")
    
    await client.run_until_disconnected()
    await application.updater.stop()
    await application.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped gracefully.")