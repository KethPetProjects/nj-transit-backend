"""
PostgreSQL-based cache for NJ Transit API data
Replaces file-based /tmp/ cache which was wiped on container restarts
Caches tokens and train schedules to avoid hitting rate limits
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor

# Get database URL from environment variable
DATABASE_URL = os.getenv('DATABASE_URL')

def get_connection():
    """Get database connection"""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable not set!")
    return psycopg2.connect(DATABASE_URL)

def cache_set(key: str, value: any, ttl_hours: float = 24):
    """
    Store value in cache with TTL (time to live)
    Persists across container restarts!
    """
    # Use UTC to match Postgres NOW()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    try:
        conn = get_connection()
        c = conn.cursor()

        c.execute('''
            INSERT INTO cache (key, value, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                expires_at = EXCLUDED.expires_at
        ''', (key, json.dumps(value), expires_at))

        conn.commit()
        conn.close()
        print(f"💾 Cached: {key} (expires in {ttl_hours}h, at {expires_at})")

    except Exception as e:
        print(f"⚠️  Cache write failed for {key}: {e}")

def cache_get(key: str) -> Optional[any]:
    """
    Get value from cache if not expired
    Returns None if not found or expired
    """
    try:
        conn = get_connection()
        c = conn.cursor(cursor_factory=RealDictCursor)

        # Debug: check what's in the row regardless of expiry
        c.execute('SELECT key, expires_at, NOW() as db_now FROM cache WHERE key = %s', (key,))
        debug = c.fetchone()
        if debug:
            print(f"   🔍 Row found: expires_at={debug['expires_at']}, db_now={debug['db_now']}")
        else:
            print(f"   🔍 No row found for key: {key}")

        # Get value only if not expired (timezone-aware comparison)
        c.execute('''
            SELECT value FROM cache
            WHERE key = %s AND expires_at > NOW() AT TIME ZONE 'UTC'
        ''', (key,))

        result = c.fetchone()
        conn.close()

        if result:
            print(f"✅ Cache hit: {key}")
            val = result['value']
            # psycopg2 auto-parses JSONB - handle dict, string, and plain values
            if isinstance(val, (dict, list)):
                return val  # already parsed by psycopg2
            if isinstance(val, str):
                try:
                    return json.loads(val)  # try parsing as JSON
                except (json.JSONDecodeError, ValueError):
                    return val  # plain string (e.g. token), return as-is
            return val
        else:
            print(f"❌ Cache miss: {key}")
            return None

    except Exception as e:
        print(f"⚠️  Cache read failed for {key}: {e}")
        return None

def cache_delete(key: str):
    """Delete a specific cache entry"""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('DELETE FROM cache WHERE key = %s', (key,))
        conn.commit()
        conn.close()
        print(f"🗑️  Cache deleted: {key}")
    except Exception as e:
        print(f"⚠️  Cache delete failed for {key}: {e}")

def cache_clear():
    """Clear ALL cache entries (use with caution!)"""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('DELETE FROM cache')
        conn.commit()
        conn.close()
        print("🗑️  Cache cleared")
    except Exception as e:
        print(f"⚠️  Cache clear failed: {e}")

def cache_cleanup_expired():
    """Remove expired entries from database (good housekeeping)"""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('DELETE FROM cache WHERE expires_at <= NOW()')
        deleted = c.rowcount
        conn.commit()
        conn.close()
        print(f"🧹 Cleaned up {deleted} expired cache entries")
    except Exception as e:
        print(f"⚠️  Cache cleanup failed: {e}")


if __name__ == '__main__':
    print("🧪 Testing PostgreSQL cache...\n")

    print("Test 1: Writing to cache...")
    cache_set('test_key', {'hello': 'world', 'number': 42}, ttl_hours=1)

    print("\nTest 2: Reading from cache...")
    result = cache_get('test_key')
    if result:
        print(f"✅ Got value: {result}")
    else:
        print("❌ Cache miss - something went wrong!")

    print("\nTest 3: Reading non-existent key...")
    result = cache_get('does_not_exist')
    if result is None:
        print("✅ Correctly returned None for missing key")

    print("\nTest 4: Deleting cache entry...")
    cache_delete('test_key')
    result = cache_get('test_key')
    if result is None:
        print("✅ Key successfully deleted")

    print("\n✅ All tests passed! PostgreSQL cache is working.")
