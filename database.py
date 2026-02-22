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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ Database initialized (PostgreSQL)")
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
        raise

def save_subscription(phone: str, morning_train: str, evening_train: str, 
                     delay_alerts: bool = True, ontime_alerts: bool = True) -> str:
    """
    Save a new subscription (status: pending)
    Returns verification code
    """
    verification_code = str(random.randint(100000, 999999))
    
    conn = get_connection()
    c = conn.cursor()
    
    try:
        c.execute('''
            INSERT INTO subscriptions 
            (phone, morning_train, evening_train, delay_alerts, ontime_alerts, verification_code, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        ''', (phone, morning_train, evening_train, delay_alerts, ontime_alerts, verification_code))
        
        conn.commit()
        print(f"📝 Subscription saved for {phone} (pending verification)")
        return verification_code
    
    except psycopg2.IntegrityError:
        # Phone already exists, update instead
        conn.rollback()
        c.execute('''
            UPDATE subscriptions 
            SET morning_train=%s, evening_train=%s, delay_alerts=%s, ontime_alerts=%s, 
                verification_code=%s, status='pending', updated_at=%s
            WHERE phone=%s
        ''', (morning_train, evening_train, delay_alerts, ontime_alerts, 
              verification_code, datetime.now(), phone))
        
        conn.commit()
        print(f"📝 Subscription updated for {phone} (pending verification)")
        return verification_code
    
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
    
    if result and result[0] == code:
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
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts
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
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts, status
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

# Initialize database on import
init_db()
