    import os
    import time
    import json
    import urllib
    import requests
    import sqlite3
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

    # Maximum number of images to download per query
    MAX_TOTAL_IMAGES = 50   
    IMAGES_PER_BATCH = 9

    # Bot password
    BOT_PASSWORD = "304050"

    # Configuration class (remains the same as in previous version)
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

        @property
        def search_keyword(self):
            return self.search_keywords

    # Database Manager (mostly unchanged from previous version)
    class DatabaseManager:
        def __init__(self, db_path='images.db'):
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.cursor = self.conn.cursor()
            self.create_tables()

        def create_tables(self):
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    filename TEXT UNIQUE,
                    url TEXT UNIQUE,
                    status TEXT DEFAULT 'pending'
                )
            ''')
            self.conn.commit()

        def insert_image(self, query, filename, url):
            try:
                self.cursor.execute('''
                    INSERT OR IGNORE INTO images (query, filename, url, status) 
                    VALUES (?, ?, ?, 'pending')
                ''', (query, filename, url))
                self.conn.commit()
            except sqlite3.IntegrityError as e:
                print(f"Error inserting image {filename}: {e}")

        def get_pending_images(self, query, limit=9):
            self.cursor.execute('''
                SELECT filename, url FROM images 
                WHERE query = ? AND status = 'pending' 
                LIMIT ?
            ''', (query, limit))
            return self.cursor.fetchall()

        def mark_image_status(self, filename, status):
            self.cursor.execute('''
                UPDATE images SET status = ? 
                WHERE filename = ?
            ''', (status, filename))
            self.conn.commit()

        def close(self):
            self.conn.close()

    # Scraper class (mostly unchanged from previous version)
    class Scraper:
        def __init__(self, config, db_manager):
            self.config = config
            self.db_manager = db_manager
            self.image_urls = []
            self.URL = None

        def download_images(self, output_path, query):
            # Clear previous image URLs to avoid duplicates
            self.image_urls.clear()
            results = self.get_urls()
            
            if not results:
                print("No results fetched from Pinterest API.")
                return 0

            os.makedirs(output_path, exist_ok=True)
            downloaded = 0

            for url in results:
                # Stop if we've reached the maximum number of images
                if downloaded >= IMAGES_PER_BATCH:
                    break

                file_name = url.split("/")[-1]
                file_path = os.path.join(output_path, file_name)

                # Store image info in database before downloading
                self.db_manager.insert_image(query, file_name, url)

                if not os.path.exists(file_path):
                    try:
                        print(f"Downloading: {url}")
                        urllib.request.urlretrieve(url, file_path)
                        downloaded += 1
                    except Exception as e:
                        print(f"Error downloading {url}: {e}")

            return downloaded

        def get_urls(self):
            try:
                r = requests.get(self.config.search_url, params={
                    "source_url": self.config.source_url,
                    "data": self.config.image_data,
                })

                if r.status_code != 200:
                    print(f"API Request Error: {r.status_code} - {r.text}")
                    return None

                jsonData = json.loads(r.content)
                resource_response = jsonData.get("resource_response", {})
                data = resource_response.get("data", {})
                results = data.get("results", [])

                for item in results:
                    try:
                        self.image_urls.append(
                            item["images"]["orig"]["url"]
                        )
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

    # Command to start the bot
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Welcome! Please enter the bot password to activate."
        )

    # Password verification handler
    async def verify_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        password = update.message.text

        if password == BOT_PASSWORD:
            user_states[user_id] = {'authenticated': True}
            await update.message.reply_text(
                "Password correct! You can now use the bot. Use /search <query> to find images."
            )
        else:
            await update.message.reply_text("Incorrect password. Access denied.")

    # Stop search handler
    async def stop_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Check if user is authenticated
        if user_id not in user_states or not user_states[user_id].get('authenticated', False):
            await update.message.reply_text("Please authenticate first by entering the password.")
            return

        # Reset user state
        if 'current_search' in user_states[user_id]:
            del user_states[user_id]['current_search']
        
        await update.message.reply_text("Search stopped. You can start a new search.")

    # Command to search for images
    async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        # Check if user is authenticated
        if user_id not in user_states or not user_states[user_id].get('authenticated', False):
            await update.message.reply_text("Please authenticate first by entering the password.")
            return

        if len(context.args) == 0:
            await update.message.reply_text("Please provide a search query. Usage: /search <query>")
            return

        # Channel details
        CHANNEL_LINK = f"https://t.me/Main_Pictures_Official"

        query = " ".join(context.args)
        chat_id = "@Main_Pictures_Official"
        output_dir = f"./images/"
        os.makedirs(output_dir, exist_ok=True)

        # Store current search in user state
        user_states[user_id]['current_search'] = {
            'query': query,
            'output_dir': output_dir,
            'total_processed': 0
        }

        # Initialize database manager
        db_manager = DatabaseManager()

        # Configure scraper
        config = Config(
            search_keywords=query,
            file_lengths=IMAGES_PER_BATCH,
            image_quality="originals",
            scroll=1000,
        )

        # Create scraper with database manager
        scraper = Scraper(config, db_manager)

        # Inform user search has started
        await update.message.reply_text(f"Starting image search for: {query}")

        # Download images
        downloaded_count = scraper.download_images(output_dir, query)

        if downloaded_count == 0:
            await update.message.reply_text(f"No images found for query: {query}")
            db_manager.close()
            return

        # Get pending images to send
        pending_images = db_manager.get_pending_images(query, IMAGES_PER_BATCH)

        # Create a stop button
        keyboard = [[InlineKeyboardButton("Stop Search", callback_data='stop_search')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Prepare media group
        media_group = []
        for index, (filename, url) in enumerate(pending_images):
            img_path = os.path.join(output_dir, filename)

            # Ensure the file exists
            if not os.path.exists(img_path):
                print(f"File not found: {img_path}")
                db_manager.mark_image_status(filename, 'error')
                continue

            # Add caption only to the last image
            caption = f"[Asosiy kanal]({CHANNEL_LINK})" if index == len(pending_images) - 1 else None
            media_group.append(InputMediaPhoto(media=open(img_path, "rb"), caption=caption, parse_mode='Markdown'))

        try:
            # Send media group
            await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        except Exception as e:
            print(f"Error sending media group: {e}")
        finally:
            # Close file handles and delete images
            for media in media_group:
                if isinstance(media.media, str):
                    continue  # Skip if media is a URL
                pass  

            # Delete images after sending
            for filename, _ in pending_images:
                img_path = os.path.join(output_dir, filename)
                if os.path.exists(img_path):
                    os.remove(img_path)

        # Update total processed images
        user_states[user_id]['current_search']['total_processed'] += len(pending_images)

        # Send stop search button
        await update.message.reply_text(
            f"Images sent! Total images processed: {user_states[user_id]['current_search']['total_processed']}",
            reply_markup=reply_markup
        )

        # Close database connection
        db_manager.close()


    # Callback query handler for stop button
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == 'stop_search':
            user_id = update.effective_user.id
            if 'current_search' in user_states.get(user_id, {}):
                del user_states[user_id]['current_search']
            await query.edit_message_text("Search stopped. You can start a new search.")

    def main():
        TELEGRAM_BOT_TOKEN = '7264528381:AAH-6DJJMaDkLEvOqq3-GaOJQrBDA3xt5kk'  # Replace with your actual bot token

        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Add handlers
        application.add_handler(CommandHand ler("start", start))
        application.add_handler(CommandHandler("search", search))
        application.add_handler(CommandHandler("stop", stop_search))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, verify_password))
        application.add_handler(CallbackQueryHandler(handle_callback))

        # Start the bot
        print("Bot is running...")
        application.run_polling()

    if __name__ == "__main__":
        main()