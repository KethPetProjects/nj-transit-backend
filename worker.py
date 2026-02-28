"""
Background Worker - Checks trains every 5 minutes
Sends alerts for delays and on-time confirmations
"""
import schedule
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_active_subscriptions
from njtransit import NJTransitAPI
from notifications import SMSService

# Container runs in UTC but all NJT/GTFS times are Eastern — use Eastern throughout
EASTERN = ZoneInfo('America/New_York')

def _now_et() -> datetime:
    """Current time as a naive Eastern datetime (matches GTFS/NJT API times)."""
    return datetime.now(EASTERN).replace(tzinfo=None)

# Initialize services
nj_transit = NJTransitAPI()
sms_service = SMSService()

# Track what alerts we've already sent (to avoid duplicates)
sent_alerts = {}

def check_trains():
    """
    Main function: Check all subscribed trains
    Runs every 5 minutes
    """
    print(f"\n🔍 [{_now_et().strftime('%H:%M:%S')}] Checking trains...")
    
    # Get all active subscriptions
    subscriptions = get_active_subscriptions()
    
    if not subscriptions:
        print("   No active subscriptions")
        return
    
    print(f"   Found {len(subscriptions)} active subscription(s)")
    
    for sub in subscriptions:
        check_subscriber_trains(sub)

def check_subscriber_trains(sub: dict):
    """Check trains for a specific subscriber"""
    phone = sub['phone']
    home_station = sub.get('station') or ''

    # Look up human-readable station names for notifications
    morning_station_name = ''
    evening_station_name = ''
    try:
        import gtfs as _gtfs
        if home_station:
            morning_station_name = _gtfs.get_station_name(home_station) or home_station
        if sub['evening_train']:
            origin_code = _gtfs.get_train_origin_njt_code(sub['evening_train'])
            if origin_code:
                evening_station_name = _gtfs.get_station_name(origin_code) or ''
    except Exception:
        pass

    # Morning train: alert fires based on departure from home station
    if sub['morning_train']:
        check_train(
            phone=phone,
            train_number=sub['morning_train'],
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts'],
            station=home_station,
            station_name=morning_station_name
        )

    # Evening train: train originates at NYC, so NJT API time is already correct
    if sub['evening_train']:
        check_train(
            phone=phone,
            train_number=sub['evening_train'],
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts'],
            station_name=evening_station_name
        )

def check_train(phone: str, train_number: str, send_delay: bool, send_ontime: bool,
               station: str = '', station_name: str = ''):
    """Check status of a specific train and send alerts if needed"""

    # Get train status from NJ Transit API
    status = nj_transit.get_train_status(train_number)

    # Create unique key to track if we've already alerted
    alert_key = f"{phone}_{train_number}_{status['status']}"

    # Check if we already sent this alert today
    if alert_key in sent_alerts:
        last_sent = sent_alerts[alert_key]
        if (_now_et() - last_sent).total_seconds() < 3600:  # Don't repeat within 1 hour
            return

    # Send delay alert
    if status['delayed'] and send_delay:
        print(f"   ⚠️  Train {train_number} delayed {status['delay_minutes']} min → Alerting {phone}")
        sms_service.send_delay_alert(phone, train_number, status['delay_minutes'], station_name)
        sent_alerts[alert_key] = _now_et()

    # Send cancellation alert
    elif status['cancelled'] and send_delay:
        print(f"   🚫 Train {train_number} cancelled → Alerting {phone}")
        sms_service.send_cancellation_alert(phone, train_number, station_name)
        sent_alerts[alert_key] = _now_et()

    # Send on-time alert (30 min before departure from the commuter's boarding station)
    elif status['on_time'] and send_ontime:
        scheduled = status['scheduled_departure']

        # If we know the commuter's boarding station, look up departure time there
        # from GTFS rather than using the train's origin departure time.
        # This ensures the 25-35 min window is relative to when THEY board, not
        # when the train left Trenton / some upstream station.
        if station:
            try:
                import gtfs as _gtfs
                gtfs_time = _gtfs.get_train_departure_at_station(train_number, station)
                if gtfs_time:
                    scheduled = gtfs_time
            except Exception as e:
                print(f"   ⚠️  GTFS departure lookup failed for {train_number} at {station}: {e}")

        if scheduled is None:
            print(f"   ⚠️  Train {train_number} has no scheduled time")
            return

        minutes_until = (scheduled - _now_et()).total_seconds() / 60

        # Send alert if train departs in 25-35 minutes (catches the 30-min window)
        if 25 <= minutes_until <= 35:
            departure_time = scheduled.strftime('%I:%M %p')
            print(f"   ✅ Train {train_number} on time → Alerting {phone}")
            sms_service.send_ontime_alert(phone, train_number, departure_time, station_name)
            sent_alerts[alert_key] = _now_et()

    else:
        print(f"   ℹ️  Train {train_number} status: {status['status']} (no alert needed)")

def cleanup_old_alerts():
    """Clean up old alert tracking (runs daily)"""
    print("🧹 Cleaning up old alert tracking...")
    current_time = _now_et()
    
    # Remove alerts older than 24 hours
    old_keys = [
        key for key, timestamp in sent_alerts.items()
        if (current_time - timestamp).total_seconds() > 86400  # 24 hours
    ]
    
    for key in old_keys:
        del sent_alerts[key]
    
    print(f"   Removed {len(old_keys)} old alert(s)")

def main():
    """Main worker loop"""
    print("\n" + "="*60)
    print("🚂 NJ TRANSIT DELAY ALERTS - BACKGROUND WORKER")
    print("="*60)
    print(f"Started at: {_now_et().strftime('%Y-%m-%d %H:%M:%S')} ET")
    print("Checking trains every 5 minutes...")
    print("Press Ctrl+C to stop")
    print("="*60 + "\n")
    
    # Schedule jobs
    schedule.every(5).minutes.do(check_trains)
    schedule.every().day.at("00:00").do(cleanup_old_alerts)
    
    # Run immediately on start
    check_trains()
    
    # Main loop
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n👋 Worker stopped")

if __name__ == "__main__":
    main()
