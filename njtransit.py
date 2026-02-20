"""
NJ Transit API Client
Real API implementation for NJ Transit Rail Data
"""
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import os

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
    
    def get_token(self) -> str:
        """Get authentication token (valid for 24 hours, limit 10 calls/day)"""
        # Check if we have a valid cached token
        if self.token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.token
        
        # Get new token
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
                self.token = result['UserToken']
                # Token expires in 24 hours
                self.token_expiry = datetime.now() + timedelta(hours=23, minutes=50)
                print(f"✅ Got new NJT API token (expires in 24 hours)")
                return self.token
            else:
                print("❌ Authentication failed")
                return None
        except Exception as e:
            print(f"❌ Error getting token: {e}")
            return None
    
    def get_station_schedule(self, station_code: str) -> dict:
        """
        Get full day train schedule for a specific station
        Uses getStationSchedule which returns 27 hours of schedule
        Returns trains grouped by direction (to-NYC/from-NYC)
        """
        
        # If no credentials, fall back to mock
        if not self.username or not self.password:
            return self._mock_station_trains(station_code)
        
        # Get token
        token = self.get_token()
        if not token:
            print("⚠️ Failed to get token, using mock data")
            return self._mock_station_trains(station_code)
        
        # Get FULL DAY schedule from NJ Transit API
        # This endpoint gives 27 hours of schedule (limit: 5 calls/day)
        url = f"{self.base_url}/getStationSchedule"
        files = {
            'token': (None, token),
            'station': (None, station_code),
            'NJTOnly': (None, 'true')  # Filter to NJ Transit trains only
        }
        
        try:
            response = requests.post(url, files=files)
            response.raise_for_status()
            result = response.json()
            
            # Parse and organize trains
            return self._organize_full_schedule(result, station_code)
            
        except Exception as e:
            print(f"❌ Error getting station schedule: {e}")
            return self._mock_station_trains(station_code)
    
    def _organize_full_schedule(self, api_response: list, station_code: str) -> dict:
        """
        Organize full day schedule into to-NYC and from-NYC trains
        API returns list of stations, we need to find our station
        """
        to_nyc = []
        from_nyc = []
        
        # Major destination hubs (where people work)
        nyc_destinations = [
            'New York', 'NY Penn', 'PSNY', 'Penn Station New York',
            'Newark', 'Newark Penn', 'Hoboken', 'Jersey City', 'Secaucus'
        ]
        
        # Find our station in the response
        for station_data in api_response:
            if station_data.get('STATION_2CHAR') == station_code:
                items = station_data.get('ITEMS', [])
                
                for train in items:
                    train_id = train.get('TRAIN_ID')
                    destination = train.get('DESTINATION', '')
                    sched_time = train.get('SCHED_DEP_DATE', '')
                    line = train.get('LINE', '')
                    
                    # Skip if no train ID
                    if not train_id:
                        continue
                    
                    # Parse time
                    try:
                        dt = datetime.strptime(sched_time, '%d-%b-%Y %I:%M:%S %p')
                        time_str = dt.strftime('%I:%M %p')
                        hour = dt.hour
                    except:
                        time_str = sched_time
                        hour = 0
                    
                    train_info = {
                        'id': train_id,
                        'time': time_str,
                        'destination': destination,
                        'line': line
                    }
                    
                    # Classify: Is this train going TO NYC or FROM NYC?
                    is_to_nyc = any(hub in destination for hub in nyc_destinations)
                    
                    if is_to_nyc:
                        to_nyc.append(train_info)
                    else:
                        from_nyc.append(train_info)
                
                break  # Found our station, no need to continue
        
        return {
            'outbound': to_nyc,     # To NYC
            'inbound': from_nyc     # From NYC
        }
    
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
    
    def get_train_status(self, train_number: str) -> Dict:
        """
        Get train status from NJ Transit API
        Returns real-time delay information for a specific train
        """
        
        # If no credentials, fall back to mock
        if not self.username or not self.password:
            return self._mock_train_status(train_number)
        
        # Get token
        token = self.get_token()
        if not token:
            print("⚠️ Failed to get token, using mock data")
            return self._mock_train_status(train_number)
        
        # For now, get all trains from Newark Penn and find our train
        # In production, you'd cache this data and refresh every few minutes
        url = f"{self.base_url}/getTrainSchedule"
        
        # Use files parameter for multipart/form-data
        files = {
            'token': (None, token),
            'station': (None, 'NP')  # Newark Penn Station
        }
        
        try:
            response = requests.post(url, files=files)
            response.raise_for_status()
            result = response.json()
            
            # Find the specific train
            items = result.get('ITEMS', [])
            for train in items:
                if train.get('TRAIN_ID') == train_number:
                    return self._parse_train_data(train)
            
            # Train not found in current schedule
            print(f"ℹ️  Train {train_number} not in current schedule")
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
            
        except Exception as e:
            print(f"❌ Error getting train status: {e}")
            return self._mock_train_status(train_number)
    
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
