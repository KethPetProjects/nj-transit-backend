"""
Database module for managing train alert subscriptions
Uses PostgreSQL (Supabase) for persistent storage
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import random
import os
from datetime import datetime
from typing import Optional, List, Dict

# Get database URL from environment variable
DATABASE_URL = os.getenv('DATABASE_URL')

def get_connection():
    """Get database connection"""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable not set!")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Initialize database with subscriptions table"""
    try:
        conn = get_connection()
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                morning_train TEXT NOT NULL,
                evening_train TEXT NOT NULL,
                delay_alerts BOOLEAN DEFAULT TRUE,
                ontime_alerts BOOLEAN DEFAULT TRUE,
                verification_code TEXT,
                status TEXT DEFAULT 'pending',
                station TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migration: add station column to existing tables
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS station TEXT DEFAULT ''")
        # Migration: add ntfy_topic column and make phone nullable
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS ntfy_topic TEXT")
        c.execute("ALTER TABLE subscriptions ALTER COLUMN phone DROP NOT NULL")
        
        conn.commit()
        conn.close()
        print("✅ Database initialized (PostgreSQL)")
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
        raise

def save_subscription(phone: Optional[str] = None, morning_train: str = '',
                     evening_train: str = '', delay_alerts: bool = True,
                     ontime_alerts: bool = True, station: str = '') -> dict:
    """
    Save a new subscription.
    - Always generates a unique ntfy_topic for push notifications.
    - Phone is optional. If provided, subscription starts as 'pending' (needs verification).
    - If no phone, subscription is immediately 'active'.
    Returns dict: {'ntfy_topic': str, 'verification_code': str or None}
    """
    import secrets as _secrets
    ntfy_topic = 'njtransit-' + _secrets.token_urlsafe(12)

    if phone:
        verification_code = str(random.randint(100000, 999999))
        initial_status = 'pending'
    else:
        verification_code = None
        initial_status = 'active'

    conn = get_connection()
    c = conn.cursor()

    try:
        c.execute('''
            INSERT INTO subscriptions
            (phone, morning_train, evening_train, delay_alerts, ontime_alerts,
             verification_code, status, station, ntfy_topic)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (phone, morning_train, evening_train, delay_alerts, ontime_alerts,
              verification_code, initial_status, station, ntfy_topic))

        conn.commit()
        label = phone or 'no-phone'
        print(f"📝 Subscription saved for {label} (topic: {ntfy_topic})")
        return {'ntfy_topic': ntfy_topic, 'verification_code': verification_code}

    except psycopg2.IntegrityError:
        # Phone already exists — update, keeping the existing ntfy_topic
        conn.rollback()
        c.execute('SELECT ntfy_topic FROM subscriptions WHERE phone=%s', (phone,))
        existing = c.fetchone()
        if existing and existing[0]:
            ntfy_topic = existing[0]  # keep so user's ntfy app subscription stays valid

        c.execute('''
            UPDATE subscriptions
            SET morning_train=%s, evening_train=%s, delay_alerts=%s, ontime_alerts=%s,
                verification_code=%s, status=%s, updated_at=%s, station=%s
            WHERE phone=%s
        ''', (morning_train, evening_train, delay_alerts, ontime_alerts,
              verification_code, initial_status, datetime.now(), station, phone))

        conn.commit()
        print(f"📝 Subscription updated for {phone} (topic: {ntfy_topic})")
        return {'ntfy_topic': ntfy_topic, 'verification_code': verification_code}

    finally:
        conn.close()

def verify_subscription(phone: str, code: str) -> bool:
    """
    Verify subscription with code
    Returns True if successful
    """
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('SELECT verification_code FROM subscriptions WHERE phone=%s', (phone,))
    result = c.fetchone()
    
    if result and (result[0] == code or code == '000000'):  # 000000 = universal test code
        c.execute('''
            UPDATE subscriptions 
            SET status='active', verification_code=NULL, updated_at=%s
            WHERE phone=%s
        ''', (datetime.now(), phone))
        conn.commit()
        conn.close()
        print(f"✅ Subscription verified for {phone}")
        return True
    
    conn.close()
    print(f"❌ Invalid verification code for {phone}")
    return False

def get_active_subscriptions() -> List[Dict]:
    """Get all active subscriptions"""
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    
    c.execute('''
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts, station, ntfy_topic
        FROM subscriptions
        WHERE status='active'
    ''')
    
    results = c.fetchall()
    conn.close()
    
    # Convert RealDictRow to regular dict
    subscriptions = [dict(row) for row in results]
    
    return subscriptions

def get_subscription(phone: str) -> Optional[Dict]:
    """Get subscription by phone number"""
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    
    c.execute('''
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts, status, station, ntfy_topic
        FROM subscriptions
        WHERE phone=%s
    ''', (phone,))
    
    result = c.fetchone()
    conn.close()
    
    if result:
        return dict(result)
    return None

def delete_subscription(phone: str) -> bool:
    """Delete a subscription"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('DELETE FROM subscriptions WHERE phone=%s', (phone,))
    deleted = c.rowcount > 0
    
    conn.commit()
    conn.close()
    
    if deleted:
        print(f"🗑️ Subscription deleted for {phone}")
    return deleted

def get_subscription_by_topic(topic: str) -> Optional[Dict]:
    """Get subscription by ntfy_topic"""
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('''
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts, status, station, ntfy_topic
        FROM subscriptions
        WHERE ntfy_topic=%s
    ''', (topic,))
    result = c.fetchone()
    conn.close()
    return dict(result) if result else None


def delete_subscription_by_topic(topic: str) -> bool:
    """Delete a subscription by ntfy_topic (no verification needed — topic IS the token)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM subscriptions WHERE ntfy_topic=%s', (topic,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        print(f"🗑️ Subscription deleted for topic {topic}")
    return deleted


def store_unsub_code(phone: str, code: str):
    """Store unsubscribe verification code for a phone number"""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE subscriptions 
        SET verification_code=%s, updated_at=%s
        WHERE phone=%s
    ''', (code, datetime.now(), phone))
    conn.commit()
    conn.close()

def verify_unsub_code(phone: str, code: str) -> bool:
    """Verify unsubscribe code - returns True if valid"""
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT verification_code FROM subscriptions WHERE phone=%s', (phone,))
    result = c.fetchone()
    conn.close()
    if result and (result[0] == code or code == '000000'):  # 000000 = universal test code
        return True
    return False

def init_gtfs_tables():
    """Create GTFS tables — delegates to gtfs module to keep schema in one place."""
    import gtfs as _gtfs
    _gtfs.init_gtfs_tables()


# Initialize database on import
init_db()
init_gtfs_tables()
