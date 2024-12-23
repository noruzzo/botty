import os
import time
import json
import urllib
import requests
import sqlite3
import asyncio
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TimedOut, RetryAfter

# Constants
IMAGES_PER_ALBUM = 9
ALBUMS_PER_BATCH = 3
TOTAL_IMAGES_PER_BATCH = IMAGES_PER_ALBUM * ALBUMS_PER_BATCH  # 27 images
BOT_PASSWORD = "304050"
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
BATCH_DELAY = 10  # seconds

class Config:
    IMAGE_SEARCH_URL = "https://tr.pinterest.com/resource/BaseSearchResource/get/?"

    def __init__(self, search_keywords="", file_lengths=9, image_quality="orig", bookmarks="", scroll=0):
        self.search_keywords = search_keywords
        self.file_lengths = file_lengths
        self.image_quality = image_quality
        self.bookmark = bookmarks
        self.scroll = str(scroll)

    @property
    def search_url(self):
        return self.IMAGE_SEARCH_URL

    @property
    def source_url(self):
        return "/search/pins/?q=" + urllib.parse.quote(self.search_keywords)

    @property
    def image_data(self):
        return (
            '{"options":{"page_size":250, "scroll":' + self.scroll + ', "query":"'
            + self.search_keywords
            + '","scope":"pins","bookmarks":["'
            + self.bookmark
            + '"],"field_set_key":"unauth_react","no_fetch_context_on_resource":false},"context":{}}'
        ).strip()

class DatabaseManager:
    def __init__(self, db_path="images.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()
        self.migrate_database()

    def create_tables(self):
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                filename TEXT UNIQUE,
                url TEXT UNIQUE,
                status TEXT DEFAULT 'pending',
                sent INTEGER DEFAULT 0
            )
            """
        )
        self.conn.commit()

    def migrate_database(self):
        cursor = self.conn.execute('PRAGMA table_info(images)')
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'sent' not in columns:
            try:
                self.cursor.execute('ALTER TABLE images ADD COLUMN sent INTEGER DEFAULT 0')
                self.conn.commit()
                print("Successfully added 'sent' column to images table")
            except sqlite3.Error as e:
                print(f"Error adding 'sent' column: {e}")

    def insert_image(self, query, filename, url):
        try:
            self.cursor.execute(
                """
                INSERT OR IGNORE INTO images (query, filename, url, status, sent)
                VALUES (?, ?, ?, 'pending', 0)
                """,
                (query, filename, url),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            print(f"Error inserting image {filename}: {e}")

    def mark_image_sent(self, filename):
        self.cursor.execute(
            """
            UPDATE images SET sent = 1
            WHERE filename = ?
            """,
            (filename,),
        )
        self.conn.commit()

    def get_unsent_images(self, query, limit):
        self.cursor.execute(
            """
            SELECT filename, url FROM images
            WHERE query = ? AND sent = 0
            LIMIT ?
            """,
            (query, limit),
        )
        return self.cursor.fetchall()

    def close(self):
        self.conn.close()

class Scraper:
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.image_urls = []
        self.URL = None

    def download_images(self, output_path, query):
        self.image_urls.clear()
        results = self.get_urls()

        if not results:
            print("No results fetched from Pinterest API.")
            return 0, []

        os.makedirs(output_path, exist_ok=True)
        downloaded = 0
        downloaded_files = []

        for url in results:
            if downloaded >= TOTAL_IMAGES_PER_BATCH:
                break

            try:
                file_name = url.split("/")[-1]
                file_path = os.path.join(output_path, file_name)

                print(f"Downloading: {url}")
                urllib.request.urlretrieve(url, file_path)
                
                downloaded += 1
                downloaded_files.append((file_name, url))
                self.db_manager.insert_image(query, file_name, url)
                
            except Exception as e:
                print(f"Error downloading {url}: {e}")
                if os.path.exists(file_path):
                    os.remove(file_path)

        return downloaded, downloaded_files

    def get_urls(self):
        try:
            r = requests.get(
                self.config.search_url,
                params={
                    "source_url": self.config.source_url,
                    "data": self.config.image_data,
                },
            )

            if r.status_code != 200:
                print(f"API Request Error: {r.status_code} - {r.text}")
                return None

            jsonData = json.loads(r.content)
            resource_response = jsonData.get("resource_response", {})
            data = resource_response.get("data", {})
            results = data.get("results", [])

            for item in results:
                try:
                    self.image_urls.append(item["images"]["orig"]["url"])
                except KeyError:
                    self.URL = None
                    self.search(item)
                    if self.URL is not None:
                        self.image_urls.append(self.URL)

            return self.image_urls
        except Exception as e:
            print(f"Error in get_urls: {e}")
            return None

    def search(self, d):
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    if k == "orig":
                        self.URL = v["url"]
                    else:
                        self.search(v)
                elif isinstance(v, list):
                    for item in v:
                        self.search(item)

# Global dictionary to track user states
user_states = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Please enter the bot password to activate."
    )

async def verify_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text

    if password == BOT_PASSWORD:
        user_states[user_id] = {"authenticated": True}
        await update.message.reply_text(
            "Password correct! You can now use the bot. Use /search <query> to find images."
        )
    else:
        await update.message.reply_text("Incorrect password. Access denied.")

async def stop_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_states or not user_states[user_id].get("authenticated", False):
        await update.message.reply_text("Please authenticate first by entering the password.")
        return

    if "current_search" in user_states[user_id]:
        del user_states[user_id]["current_search"]

    await update.message.reply_text("Search stopped. You can start a new search.")

async def send_image_batch(context: ContextTypes.DEFAULT_TYPE, chat_id, media_group, channel_link, retry_count=0):
    if media_group:
        try:
            # Create the media group with caption only on the first image
            media = []
            for i, photo in enumerate(media_group):
                if i == 0:  # Add caption to first image only
                    media.append(InputMediaPhoto(
                        media=photo.media,
                        caption=f"[Asosiy kanal]({channel_link})",
                        parse_mode="Markdown"
                    ))
                else:
                    media.append(photo)
            
            try:
                await context.bot.send_media_group(chat_id=chat_id, media=media)
                await asyncio.sleep(BATCH_DELAY)  # Add delay after successful send
                return True
            except (TimedOut, RetryAfter) as e:
                if retry_count < MAX_RETRIES:
                    print(f"Timeout occurred, retrying... (Attempt {retry_count + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(RETRY_DELAY)
                    return await send_image_batch(context, chat_id, media_group, channel_link, retry_count + 1)
                else:
                    print(f"Failed to send media group after {MAX_RETRIES} attempts")
                    return False
        except Exception as e:
            print(f"Error sending media group: {e}")
            return False
        finally:
            # Close all open files
            for media_item in media_group:
                if hasattr(media_item.media, 'close'):
                    media_item.media.close()

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_states or not user_states[user_id].get("authenticated", False):
        await update.message.reply_text("Please authenticate first by entering the password.")
        return

    if len(context.args) == 0:
        await update.message.reply_text("Please provide a search query. Usage: /search <query>")
        return

    CHANNEL_LINK = "https://t.me/Main_Pictures_Official"
    query = " ".join(context.args)
    chat_id = "@Main_Pictures_Official"
    output_dir = f"./images/"
    os.makedirs(output_dir, exist_ok=True)

    user_states[user_id] = {
        "authenticated": True,
        "current_search": {
            "query": query,
            "output_dir": output_dir,
            "total_processed": 0,
        }
    }

    db_manager = DatabaseManager()
    config = Config(
        search_keywords=query,
        file_lengths=TOTAL_IMAGES_PER_BATCH,
        image_quality="originals",
        scroll=1000,
    )

    scraper = Scraper(config, db_manager)
    await update.message.reply_text(f"Starting image search for: {query}")
    
    downloaded_count, downloaded_files = scraper.download_images(output_dir, query)

    if downloaded_count == 0:
        await update.message.reply_text(f"No images found for query: {query}")
        db_manager.close()
        return

    successful_sends = 0
    for i in range(0, len(downloaded_files), IMAGES_PER_ALBUM):
        batch = downloaded_files[i:i + IMAGES_PER_ALBUM]
        media_group = []
        
        for filename, url in batch:
            img_path = os.path.join(output_dir, filename)
            if not os.path.exists(img_path):
                continue

            try:
                media_group.append(InputMediaPhoto(media=open(img_path, "rb")))
            except Exception as e:
                print(f"Error adding image to media group: {e}")
                continue

        if media_group:
            success = await send_image_batch(context, chat_id, media_group, CHANNEL_LINK)
            if success:
                successful_sends += len(media_group)
                # Mark images as sent in database
                for filename, _ in batch:
                    db_manager.mark_image_sent(filename)

        # Clean up files after sending
        for filename, _ in batch:
            img_path = os.path.join(output_dir, filename)
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as e:
                    print(f"Error removing file {img_path}: {e}")

    user_states[user_id]["current_search"]["total_processed"] += successful_sends

    keyboard = [
        [
            InlineKeyboardButton("Continue", callback_data="continue_search"),
            InlineKeyboardButton("Stop", callback_data="stop_search")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Batch complete! Total images processed: {user_states[user_id]['current_search']['total_processed']}",
        reply_markup=reply_markup,
    )

    db_manager.close()

async def continue_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if user_id not in user_states or "current_search" not in user_states[user_id]:
        await query.message.reply_text("No active search found. Please start a new search with /search <query>")
        return

    current_search = user_states[user_id]["current_search"]
    search_query = current_search["query"]
    
    # Set up the context args
    context.args = search_query.split()
    
    # Create a mock message dictionary
    mock_message = {
        'message_id': query.message.message_id,
        'date': query.message.date,
        'chat': query.message.chat.to_dict(),
        'text': f"/search {search_query}",
        'from': query.message.from_user.to_dict() if query.message.from_user else None,
    }
    
    # Create new update with mock message
    new_update = Update(
        update_id=update.update_id,
        message=Message.de_json(mock_message, context.bot)
    )
    
    await search(new_update, context)
    
    await search(new_update, context)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "stop_search":
        await stop_search(update, context)
    elif query.data == "continue_search":
        await continue_search(update, context)

def main():
    db_manager = DatabaseManager()
    db_manager.create_tables()
    db_manager.close()

    app = Application.builder().token("7264528381:AAH-6DJJMaDkLEvOqq3-GaOJQrBDA3xt5kk").build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("stop", stop_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, verify_password))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()