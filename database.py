"""
Database module for managing train alert subscriptions
Uses SQLite for simplicity
"""
import sqlite3
import random
from datetime import datetime
from typing import Optional, List, Dict

DB_FILE = 'subscriptions.db'

def init_db():
    """Initialize database with subscriptions table"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL UNIQUE,
            morning_train TEXT NOT NULL,
            evening_train TEXT NOT NULL,
            delay_alerts BOOLEAN DEFAULT 1,
            ontime_alerts BOOLEAN DEFAULT 1,
            verification_code TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

def save_subscription(phone: str, morning_train: str, evening_train: str, 
                     delay_alerts: bool = True, ontime_alerts: bool = True) -> str:
    """
    Save a new subscription (status: pending)
    Returns verification code
    """
    verification_code = str(random.randint(100000, 999999))
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        c.execute('''
            INSERT INTO subscriptions 
            (phone, morning_train, evening_train, delay_alerts, ontime_alerts, verification_code, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ''', (phone, morning_train, evening_train, delay_alerts, ontime_alerts, verification_code))
        
        conn.commit()
        print(f"📝 Subscription saved for {phone} (pending verification)")
        return verification_code
    
    except sqlite3.IntegrityError:
        # Phone already exists, update instead
        c.execute('''
            UPDATE subscriptions 
            SET morning_train=?, evening_train=?, delay_alerts=?, ontime_alerts=?, 
                verification_code=?, status='pending', updated_at=?
            WHERE phone=?
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT verification_code FROM subscriptions WHERE phone=?', (phone,))
    result = c.fetchone()
    
    if result and result[0] == code:
        c.execute('''
            UPDATE subscriptions 
            SET status='active', verification_code=NULL, updated_at=?
            WHERE phone=?
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts
        FROM subscriptions 
        WHERE status='active'
    ''')
    
    results = c.fetchall()
    conn.close()
    
    subscriptions = []
    for row in results:
        subscriptions.append({
            'phone': row[0],
            'morning_train': row[1],
            'evening_train': row[2],
            'delay_alerts': bool(row[3]),
            'ontime_alerts': bool(row[4])
        })
    
    return subscriptions

def get_subscription(phone: str) -> Optional[Dict]:
    """Get subscription by phone number"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        SELECT phone, morning_train, evening_train, delay_alerts, ontime_alerts, status
        FROM subscriptions 
        WHERE phone=?
    ''', (phone,))
    
    result = c.fetchone()
    conn.close()
    
    if result:
        return {
            'phone': result[0],
            'morning_train': result[1],
            'evening_train': result[2],
            'delay_alerts': bool(result[3]),
            'ontime_alerts': bool(result[4]),
            'status': result[5]
        }
    return None

def delete_subscription(phone: str) -> bool:
    """Delete a subscription"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('DELETE FROM subscriptions WHERE phone=?', (phone,))
    deleted = c.rowcount > 0
    
    conn.commit()
    conn.close()
    
    if deleted:
        print(f"🗑️ Subscription deleted for {phone}")
    return deleted

# Initialize database on import
init_db()
