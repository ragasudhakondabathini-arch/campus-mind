import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sessions.db")

def get_db():
    """
    Returns an async context manager for aiosqlite connection.
    Usage:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            ...
    """
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("PRAGMA journal_mode = WAL;")
        
        # Sessions Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                faculty_id TEXT NOT NULL,
                faculty_name TEXT,
                subject TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                planned_duration INTEGER NOT NULL,
                actual_duration REAL DEFAULT 0,
                session_duration_minutes INTEGER NOT NULL,
                disconnect_grace_seconds INTEGER NOT NULL,
                hotspot_ssid TEXT,
                total_joined INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );
        """)

        # Safe migration for existing databases
        try:
            await db.execute("ALTER TABLE sessions ADD COLUMN faculty_name TEXT;")
        except Exception:
            pass
        
        # Session Attendees Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_attendees (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                student_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                device_mac TEXT,
                device_ip TEXT,
                join_time REAL NOT NULL,
                leave_time REAL,
                total_minutes REAL DEFAULT 0.0,
                total_seconds INTEGER DEFAULT 0,
                current_status TEXT DEFAULT 'JOINED',
                final_status TEXT DEFAULT 'PENDING',
                last_seen REAL NOT NULL,
                disconnect_time REAL,
                UNIQUE(session_id, student_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
        """)
        
        # Connection Events Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS connection_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                student_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                details TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
        """)
        
        # Faculty Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS faculty_users (
                faculty_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        # Indexes for fast querying
        await db.execute("CREATE INDEX IF NOT EXISTS idx_attendees_session ON session_attendees(session_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON connection_events(session_id, student_id);")

        await db.commit()
