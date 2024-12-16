import sqlite3
import os

class DatabaseManager:
    def __init__(self, db_path='images.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.migrate_database()

    def migrate_database(self):
        # Check current table schema
        try:
            # Try to query the status column
            self.cursor.execute("SELECT status FROM images LIMIT 1")
            return  # Column exists, no migration needed
        except sqlite3.OperationalError:
            # Column doesn't exist, we need to migrate
            self.migrate_table()

    def migrate_table(self):
        # Backup existing database
        backup_path = f"{self.db_path}.backup"
        if os.path.exists(self.db_path):
            os.rename(self.db_path, backup_path)

        # Reconnect to create a new database
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()

        # Create new table with all columns
        self.cursor.execute('''
            CREATE TABLE images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                filename TEXT UNIQUE,
                url TEXT UNIQUE,
                status TEXT DEFAULT 'pending'
            )
        ''')
        self.conn.commit()

        # Optionally, restore data from backup if needed
        if os.path.exists(backup_path):
            try:
                old_conn = sqlite3.connect(backup_path)
                old_cursor = old_conn.cursor()
                
                # Fetch existing data
                old_cursor.execute("SELECT query, filename, url FROM images")
                existing_data = old_cursor.fetchall()
                
                # Insert existing data into new table
                for query, filename, url in existing_data:
                    self.cursor.execute('''
                        INSERT OR IGNORE INTO images (query, filename, url, status) 
                        VALUES (?, ?, ?, 'pending')
                    ''', (query, filename, url))
                
                self.conn.commit()
                old_conn.close()
            except Exception as e:
                print(f"Error migrating data: {e}")

    def create_tables(self):
        # This method is now redundant but kept for compatibility
        pass

    def insert_image(self, query, filename, url):
        try:
            self.cursor.execute('''
                INSERT OR IGNORE INTO images (query, filename, url, status) 
                VALUES (?, ?, ?, 'pending')
            ''', (query, filename, url))
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            print(f"Error inserting image {filename}: {e}")
        except sqlite3.OperationalError as e:
            print(f"Operational error: {e}")
            # Attempt to migrate if there's a structural issue
            self.migrate_database()

    def get_pending_images(self, query, limit=9):
        self.cursor.execute('''
            SELECT filename FROM images 
            WHERE query = ? AND status = 'pending' 
            LIMIT ?
        ''', (query, limit))
        return [row[0] for row in self.cursor.fetchall()]

    def mark_image_status(self, filename, status):
        self.cursor.execute('''
            UPDATE images SET status = ? 
            WHERE filename = ?
        ''', (status, filename))
        self.conn.commit()

    def close(self):
        self.conn.close()