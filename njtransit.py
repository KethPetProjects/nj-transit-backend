"""
NJ Transit API Client
Real API implementation for NJ Transit Rail Data
"""
import requests
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import os
from cache import cache_get, cache_set

# Per-cycle station response cache — keyed by origin_station, value is (timestamp, raw ITEMS list).
# Cleared at the start of each worker check cycle via clear_cycle_cache().
# Means one getTrainSchedule call per unique station per cycle, regardless of how many
# subscribers board at that station or how many trains they track.
_station_cache: Dict[str, tuple] = {}

class NJTransitAPI:
    """NJ Transit API Client"""
    
    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        # Load from environment variables if not provided
        self.username = username or os.getenv('NJT_USERNAME')
        self.password = password or os.getenv('NJT_PASSWORD')
        
        # Use test environment by default, can be overridden
        self.base_url = os.getenv('NJT_API_URL', "https://testraildata.njtransit.com/api/TrainData")
        
        self.token = None
        self.token_expiry = None
        
        if self.username and self.password:
            print("🚂 NJ Transit API initialized (REAL MODE)")
        else:
            print("⚠️  NJ Transit API credentials not found in environment variables")
            print("🚂 NJ Transit API initialized (MOCK MODE - no credentials)")
    
    def clear_cycle_cache(self):
        """Clear the per-cycle station cache. Call at the start of each worker check cycle."""
        _station_cache.clear()

    def get_token(self) -> str:
        """Get authentication token (valid for 24 hours, limit 10 calls/day)"""
        
        # Check cache first
        cached_token = cache_get('nj_transit_token')
        if cached_token:
            print("✅ Using cached NJT API token")
            return cached_token
        
        # Get new token from API
        url = f"{self.base_url}/getToken"
        
        # Use files parameter for multipart/form-data (like curl -F)
        files = {
            'username': (None, self.username),
            'password': (None, self.password)
        }
        
        try:
            response = requests.post(url, files=files)
            response.raise_for_status()
            result = response.json()
            
            if result.get('Authenticated') == 'True':
                token = result['UserToken']
                
                # Cache token for 23.5 hours (expires in 24h, we refresh early)
                cache_set('nj_transit_token', token, ttl_hours=23.5)
                
                print(f"✅ Got new NJT API token (cached for 23.5 hours)")
                return token
            else:
                print("❌ Authentication failed")
                return None
        except Exception as e:
            print(f"❌ Error getting token: {e}")
            return None
    
    def get_station_schedule(self, station_code: str, query_date=None) -> dict:
        """
        Get full day train schedule for a specific station.
        Tries GTFS DB first (no rate limit, fixes pass-through bug).
        Falls back to NJT API → mock if GTFS data is unavailable.
        query_date: date object; defaults to today when None.
        """
        import gtfs
        from datetime import date as _date
        if query_date is None:
            query_date = _date.today()

        # 1. Try GTFS (preferred — unlimited, accurate stop filtering)
        gtfs_result = gtfs.get_station_schedule(station_code, query_date=query_date)
        if gtfs_result['outbound'] or gtfs_result['inbound']:
            print(f"✅ Served {station_code} schedule from GTFS ({query_date})")
            return gtfs_result

        print(f"⚠️ GTFS returned no trains for {station_code} — falling back to NJT API")

        # 2. Fall back to NJT API (5 calls/day rate limit — use sparingly)
        # Check cache first to protect the rate limit
        cache_key = f'station_schedule_{station_code}'
        cached_schedule = cache_get(cache_key)
        if cached_schedule:
            print(f"✅ Using cached NJT API schedule for {station_code}")
            return cached_schedule

        if not self.username or not self.password:
            return self._mock_station_trains(station_code)

        token = self.get_token()
        if not token:
            print("⚠️ Failed to get token, using mock data")
            return self._mock_station_trains(station_code)

        url = f"{self.base_url}/getStationSchedule"
        files = {
            'token': (None, token),
            'station': (None, station_code),
            'NJTOnly': (None, 'true'),
        }

        try:
            response = requests.post(url, files=files)
            response.raise_for_status()
            result = response.json()

            if not isinstance(result, list):
                print(f"❌ Unexpected NJT API response for {station_code}: {str(result)[:200]}")
                return self._mock_station_trains(station_code)

            schedule = self._organize_full_schedule(result, station_code)

            if not schedule['outbound'] and not schedule['inbound']:
                print(f"⚠️ Station {station_code} not found in NJT API response")
                return {'outbound': [], 'inbound': []}

            cache_set(cache_key, schedule, ttl_hours=24)
            print(f"💾 Cached NJT API schedule for {station_code} (24 hours)")
            return schedule

        except Exception as e:
            print(f"❌ NJT API error for {station_code}: {e}")
            return self._mock_station_trains(station_code)

    def _parse_station_data(self, station_data: dict) -> dict:
        """
        Parse a single station's train items into outbound/inbound lists.
        """
        to_nyc = []
        from_nyc = []

        # Major destination hubs (where people work)
        nyc_destinations = [
            'New York', 'NY Penn', 'PSNY', 'Penn Station New York',
            'Newark', 'Newark Penn', 'Hoboken', 'Jersey City', 'Secaucus'
        ]

        items = station_data.get('ITEMS', [])
        for train in items:
            train_id = train.get('TRAIN_ID')
            destination = train.get('DESTINATION', '')
            sched_time = train.get('SCHED_DEP_DATE', '')
            line = train.get('LINE', '')

            if not train_id:
                continue

            try:
                dt = datetime.strptime(sched_time, '%d-%b-%Y %I:%M:%S %p')
                time_str = dt.strftime('%I:%M %p')
            except:
                time_str = sched_time

            train_info = {
                'id': train_id,
                'time': time_str,
                'destination': destination,
                'line': line
            }

            is_to_nyc = any(hub in destination for hub in nyc_destinations)
            if is_to_nyc:
                to_nyc.append(train_info)
            else:
                from_nyc.append(train_info)

        return {
            'outbound': to_nyc,
            'inbound': from_nyc
        }

    def _organize_full_schedule(self, api_response: list, station_code: str) -> dict:
        """
        Organize full day schedule into to-NYC and from-NYC trains.
        Kept for backward compatibility — prefer _cache_all_stations + cache_get.
        """
        for station_data in api_response:
            if station_data.get('STATION_2CHAR') == station_code:
                return self._parse_station_data(station_data)

        return {'outbound': [], 'inbound': []}
    
    def _mock_station_trains(self, station_code: str) -> dict:
        """Mock trains when API not available"""
        return {
            'outbound': [
                {'id': '3817', 'time': '06:45 AM', 'destination': 'New York Penn'},
                {'id': '3221', 'time': '07:15 AM', 'destination': 'New York Penn'},
                {'id': '3225', 'time': '07:45 AM', 'destination': 'New York Penn'},
            ],
            'inbound': [
                {'id': '3826', 'time': '05:15 PM', 'destination': station_code},
                {'id': '3830', 'time': '05:45 PM', 'destination': station_code},
                {'id': '5711', 'time': '06:15 PM', 'destination': station_code},
            ]
        }
    
    def get_service_alerts(self, station: str = 'NP') -> list:
        """Fetch service alerts from getStationMSG. Returns raw list of alert dicts."""
        if not self.username or not self.password:
            return []
        token = self.get_token()
        if not token:
            return []
        try:
            response = requests.post(
                f"{self.base_url}/getStationMSG",
                files={'token': (None, token), 'station': (None, station)}
            )
            response.raise_for_status()
            result = response.json()
            return result if isinstance(result, list) else []
        except Exception as e:
            print(f"⚠️ getStationMSG error: {e}")
            return []

    def get_train_status(self, train_number: str, query_station: str = None) -> Dict:
        """
        Get train status from NJ Transit API
        Returns real-time delay information for a specific train.

        query_station: if provided, query getTrainSchedule from this station
        directly (used for morning trains so track/time reflect the user's
        boarding stop, not the train's origin further down the line).
        If omitted, falls back to GTFS origin lookup (correct for evening
        trains where the user boards at the origin: NY Penn / Hoboken).
        """

        # If no credentials, fall back to mock
        if not self.username or not self.password:
            return self._mock_train_status(train_number)

        # Get token
        token = self.get_token()
        if not token:
            print("⚠️ Failed to get token, using mock data")
            return self._mock_train_status(train_number)

        if query_station:
            # Morning trains: query from user's boarding station so track and
            # scheduled departure reflect their actual stop, not the origin.
            origin_station = query_station
            print(f"   🔍 Querying {train_number} from boarding station {query_station}")
        else:
            # Evening trains: user boards at origin (NY Penn / Hoboken) — use GTFS.
            origin_station = 'NP'  # fallback: Newark Penn
            try:
                import gtfs as _gtfs
                gtfs_origin = _gtfs.get_train_origin_njt_code(train_number)
                if gtfs_origin:
                    origin_station = gtfs_origin
            except Exception as _e:
                print(f"⚠️ GTFS origin lookup failed for {train_number}: {_e}")

        # Check cycle cache — one getTrainSchedule call per station per cycle
        cached = _station_cache.get(origin_station)
        if cached:
            items = cached
            print(f"   ✅ [{origin_station}] Using cycle-cached station data for train {train_number}")
        else:
            url = f"{self.base_url}/getTrainSchedule"
            files = {
                'token': (None, token),
                'station': (None, origin_station)
            }
            try:
                response = requests.post(url, files=files)
                response.raise_for_status()
                items = response.json().get('ITEMS', [])
                _station_cache[origin_station] = items
                print(f"   🌐 [{origin_station}] Fetched fresh station data ({len(items)} trains)")
            except Exception as e:
                print(f"❌ Error getting train status: {e}")
                return self._mock_train_status(train_number)

        for train in items:
            if train.get('TRAIN_ID') == train_number:
                return self._parse_train_data(train)

        # Train not found in current schedule
        print(f"ℹ️  Train {train_number} not in current schedule at {origin_station}")
        return {
            'train_number': train_number,
            'scheduled_departure': None,
            'actual_departure': None,
            'delay_minutes': 0,
            'on_time': True,
            'delayed': False,
            'cancelled': False,
            'status': 'not_scheduled'
        }
    
    def _parse_train_data(self, train: Dict) -> Dict:
        """Parse train data from API response"""
        sec_late = int(train.get('SEC_LATE', 0))
        delay_minutes = sec_late // 60
        
        status_text = train.get('STATUS', '').lower()
        
        # Parse scheduled time
        sched_dep_str = train.get('SCHED_DEP_DATE', '')
        try:
            scheduled = datetime.strptime(sched_dep_str, '%d-%b-%Y %I:%M:%S %p')
        except:
            scheduled = datetime.now() + timedelta(minutes=30)
        
        # Calculate actual departure
        actual = scheduled + timedelta(seconds=sec_late)
        
        # Determine status
        cancelled = 'cancel' in status_text
        delayed = sec_late > 300  # More than 5 minutes late
        on_time = not cancelled and not delayed
        
        return {
            'train_number': train.get('TRAIN_ID'),
            'scheduled_departure': scheduled,
            'actual_departure': actual if not cancelled else None,
            'delay_minutes': delay_minutes,
            'on_time': on_time,
            'delayed': delayed,
            'cancelled': cancelled,
            'status': 'on_time' if on_time else ('delayed' if delayed else 'cancelled'),
            'destination': train.get('DESTINATION', ''),
            'track': train.get('TRACK', ''),
            'line': train.get('LINE', '')
        }
    
    def _mock_train_status(self, train_number: str) -> Dict:
        """Fallback mock data when API not available"""
        import random
        scenario = random.choice(['on_time', 'delayed', 'cancelled', 'on_time', 'on_time'])
        
        scheduled_time = datetime.now() + timedelta(minutes=30)
        
        if scenario == 'on_time':
            return {
                'train_number': train_number,
                'scheduled_departure': scheduled_time,
                'actual_departure': scheduled_time,
                'delay_minutes': 0,
                'on_time': True,
                'delayed': False,
                'cancelled': False,
                'status': 'on_time'
            }
        
        elif scenario == 'delayed':
            delay = random.randint(5, 30)
            return {
                'train_number': train_number,
                'scheduled_departure': scheduled_time,
                'actual_departure': scheduled_time + timedelta(minutes=delay),
                'delay_minutes': delay,
                'on_time': False,
                'delayed': True,
                'cancelled': False,
                'status': f'delayed_{delay}_min'
            }
        
        else:  # cancelled
            return {
                'train_number': train_number,
                'scheduled_departure': scheduled_time,
                'actual_departure': None,
                'delay_minutes': 0,
                'on_time': False,
                'delayed': False,
                'cancelled': True,
                'status': 'cancelled'
            }
    
    def get_available_trains(self, direction: str = 'outbound') -> List[Dict]:
        """
        Get list of available trains
        Uses real API if credentials available, otherwise mock data
        """
        
        # If no credentials, return mock data
        if not self.username or not self.password:
            return self._mock_available_trains(direction)
        
        # In a real implementation, you might want to:
        # 1. Get full schedule for the day
        # 2. Filter by direction/time
        # 3. Cache results
        
        # For now, return common train numbers
        # In production, parse from getStationSchedule
        
        if direction == 'outbound':
            return [
                {'number': '3800', 'time': '06:05', 'destination': 'NY Penn'},
                {'number': '3802', 'time': '06:35', 'destination': 'NY Penn'},
                {'number': '3804', 'time': '07:05', 'destination': 'NY Penn'},
                {'number': '3806', 'time': '07:35', 'destination': 'NY Penn'},
                {'number': '3808', 'time': '08:05', 'destination': 'NY Penn'},
                {'number': '3810', 'time': '08:35', 'destination': 'NY Penn'},
            ]
        else:
            return [
                {'number': '3801', 'time': '16:15', 'destination': 'Trenton'},
                {'number': '3803', 'time': '16:45', 'destination': 'Trenton'},
                {'number': '3805', 'time': '17:15', 'destination': 'Trenton'},
                {'number': '3807', 'time': '17:45', 'destination': 'Trenton'},
                {'number': '3809', 'time': '18:15', 'destination': 'Trenton'},
                {'number': '3811', 'time': '18:45', 'destination': 'Trenton'},
            ]
    
    def _mock_available_trains(self, direction: str) -> List[Dict]:
        """Mock train list for when API not available"""
        if direction == 'outbound':
            return [
                {'number': '3800', 'time': '06:05', 'destination': 'NY Penn'},
                {'number': '3802', 'time': '06:35', 'destination': 'NY Penn'},
                {'number': '3804', 'time': '07:05', 'destination': 'NY Penn'},
                {'number': '3806', 'time': '07:35', 'destination': 'NY Penn'},
                {'number': '3808', 'time': '08:05', 'destination': 'NY Penn'},
                {'number': '3810', 'time': '08:35', 'destination': 'NY Penn'},
            ]
        else:
            return [
                {'number': '3801', 'time': '16:15', 'destination': 'Trenton'},
                {'number': '3803', 'time': '16:45', 'destination': 'Trenton'},
                {'number': '3805', 'time': '17:15', 'destination': 'Trenton'},
                {'number': '3807', 'time': '17:45', 'destination': 'Trenton'},
                {'number': '3809', 'time': '18:15', 'destination': 'Trenton'},
                {'number': '3811', 'time': '18:45', 'destination': 'Trenton'},
            ]

# Example usage:
if __name__ == '__main__':
    # Test with credentials
    api = NJTransitAPI(username='YOUR_USERNAME', password='YOUR_PASSWORD')
    
    print("\n📋 Available morning trains:")
    for train in api.get_available_trains('outbound'):
        print(f"  Train {train['number']} - {train['time']} to {train['destination']}")
    
    print("\n🔍 Checking train status:")
    status = api.get_train_status('3804')
    print(f"  Train {status['train_number']}: {status['status']}")
    if status['delayed']:
        print(f"  Delayed by {status['delay_minutes']} minutes")
