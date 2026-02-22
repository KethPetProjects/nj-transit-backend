"""
Simple file-based cache for NJ Transit API data
Caches tokens and train schedules to avoid hitting rate limits
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional, Dict

# Cache directory
CACHE_DIR = '/tmp/nj_transit_cache'

# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

def _get_cache_path(key: str) -> str:
    """Get file path for cache key"""
    # Sanitize key for filename
    safe_key = key.replace('/', '_').replace(':', '_')
    return os.path.join(CACHE_DIR, f'{safe_key}.json')

def cache_set(key: str, value: any, ttl_hours: int = 24):
    """
    Store value in cache with TTL (time to live)
    
    Args:
        key: Cache key
        value: Value to cache (must be JSON serializable)
        ttl_hours: Time to live in hours (default: 24)
    """
    cache_data = {
        'value': value,
        'expires_at': (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
    }
    
    cache_path = _get_cache_path(key)
    
    try:
        with open(cache_path, 'w') as f:
            json.dump(cache_data, f)
        print(f"💾 Cached: {key} (expires in {ttl_hours}h)")
    except Exception as e:
        print(f"⚠️  Cache write failed for {key}: {e}")

def cache_get(key: str) -> Optional[any]:
    """
    Get value from cache if not expired
    
    Args:
        key: Cache key
        
    Returns:
        Cached value if found and not expired, None otherwise
    """
    cache_path = _get_cache_path(key)
    
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, 'r') as f:
            cache_data = json.load(f)
        
        # Check if expired
        expires_at = datetime.fromisoformat(cache_data['expires_at'])
        if datetime.now() > expires_at:
            print(f"⏰ Cache expired: {key}")
            os.remove(cache_path)
            return None
        
        print(f"✅ Cache hit: {key}")
        return cache_data['value']
    
    except Exception as e:
        print(f"⚠️  Cache read failed for {key}: {e}")
        return None

def cache_delete(key: str):
    """Delete a cache entry"""
    cache_path = _get_cache_path(key)
    
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
            print(f"🗑️  Cache deleted: {key}")
    except Exception as e:
        print(f"⚠️  Cache delete failed for {key}: {e}")

def cache_clear():
    """Clear all cache entries"""
    try:
        for filename in os.listdir(CACHE_DIR):
            file_path = os.path.join(CACHE_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        print("🗑️  Cache cleared")
    except Exception as e:
        print(f"⚠️  Cache clear failed: {e}")
