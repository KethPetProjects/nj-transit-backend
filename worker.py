"""
Background Worker - Checks trains every 2 minutes
Sends alerts for delays, on-time confirmations, and track assignments
"""
import schedule
import time
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from database import get_active_subscriptions
from njtransit import NJTransitAPI
from notifications import SMSService
from cache import cache_get

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

# Track last known track assignment per (phone, train) — notify on first assignment
last_track = {}

# Track service alerts already sent this session (dedup)
sent_service_alerts = set()

ACTIVE_HOUR_START = 6   # 6 AM ET
ACTIVE_HOUR_END   = 20  # 8 PM ET

def _parse_utc_date(date_str: str):
    """Parse MSG_PUBDATE_UTC string (e.g. '3/2/2026 9:52:00 PM') to UTC datetime."""
    try:
        return datetime.strptime(date_str, '%m/%d/%Y %I:%M:%S %p').replace(tzinfo=timezone.utc)
    except Exception:
        return None


def check_service_alerts():
    """
    Check NJT getStationMSG for system-wide service alerts and push to all subscribers.
    Gated by 'service_alerts_enabled' flag in DB cache — off by default.
    Only sends alerts published within the last 2 hours (prevents re-sending persistent alerts).
    """
    if not cache_get('service_alerts_enabled'):
        return

    alerts = nj_transit.get_service_alerts('NP')
    if not alerts:
        return

    subscriptions = get_active_subscriptions()
    if not subscriptions:
        return

    now_utc = datetime.now(timezone.utc)

    for alert in alerts:
        # System-wide only — line-scoped alerts have a non-blank MSG_LINE_SCOPE
        if alert.get('MSG_LINE_SCOPE', '').strip():
            continue

        # Freshness check — skip alerts older than 2 hours
        pub_date = _parse_utc_date(alert.get('MSG_PUBDATE_UTC', ''))
        if not pub_date or (now_utc - pub_date).total_seconds() > 7200:
            continue

        # Dedup key — prefer MSG_ID, fall back to text hash
        msg_id = alert.get('MSG_ID', '').strip()
        dedup_key = f"svc_{msg_id}" if msg_id else \
                    f"svc_{hashlib.md5(alert.get('MSG_TEXT','')[:120].encode()).hexdigest()}"

        if dedup_key in sent_service_alerts:
            continue

        msg_text = alert.get('MSG_TEXT', '').strip()
        msg_url = alert.get('MSG_URL', '').strip() or None
        if not msg_text:
            continue

        print(f"   📢 Service alert → sending to {len(subscriptions)} subscriber(s): {msg_text[:80]}...")
        for sub in subscriptions:
            ntfy_topic = sub.get('ntfy_topic')
            if ntfy_topic:
                sms_service._send_ntfy(
                    title="NJ Transit Service Alert",
                    message=msg_text,
                    priority="high",
                    topic=ntfy_topic,
                    click_url=msg_url
                )

        sent_service_alerts.add(dedup_key)


def check_trains():
    """
    Main function: Check all subscribed trains
    Runs every 2 minutes, but only between 6 AM and 8 PM ET.
    """
    now = _now_et()
    if not (ACTIVE_HOUR_START <= now.hour < ACTIVE_HOUR_END):
        print(f"   ⏸  [{now.strftime('%H:%M')} ET] Outside active hours ({ACTIVE_HOUR_START} AM–{ACTIVE_HOUR_END - 12} PM) — skipping")
        return

    print(f"\n🔍 [{now.strftime('%H:%M:%S')}] Checking trains...")
    
    # Get all active subscriptions
    subscriptions = get_active_subscriptions()
    
    if not subscriptions:
        print("   No active subscriptions")
        return
    
    print(f"   Found {len(subscriptions)} active subscription(s)")

    check_service_alerts()

    for sub in subscriptions:
        check_subscriber_trains(sub)

def check_subscriber_trains(sub: dict):
    """Check trains for a specific subscriber"""
    phone = sub['phone']
    home_station = sub.get('station') or ''

    ntfy_topic = sub.get('ntfy_topic') or None
    manage_url = f"https://black-plant-0162ad510.4.azurestaticapps.net/?topic={ntfy_topic}" if ntfy_topic else None

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
            station_name=morning_station_name,
            ntfy_topic=ntfy_topic,
            manage_url=manage_url
        )

    # Evening train: train originates at NYC, so NJT API time is already correct
    if sub['evening_train']:
        check_train(
            phone=phone,
            train_number=sub['evening_train'],
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts'],
            station_name=evening_station_name,
            ntfy_topic=ntfy_topic,
            manage_url=manage_url
        )

def check_train(phone: str, train_number: str, send_delay: bool, send_ontime: bool,
               station: str = '', station_name: str = '',
               ntfy_topic: str = None, manage_url: str = None):
    """Check status of a specific train and send alerts if needed"""

    # Get train status from NJ Transit API
    status = nj_transit.get_train_status(train_number)

    # Track notification — fire once when track is first assigned
    track = status.get('track', '').strip()
    track_key = f"{phone}_{train_number}"
    prev_track = last_track.get(track_key, '')
    if track and track != prev_track and ntfy_topic:
        last_track[track_key] = track
        departure_time = ''
        if status.get('scheduled_departure'):
            departure_time = f" | Departs {status['scheduled_departure'].strftime('%I:%M %p')}"
        loc = f" at {station_name}" if station_name else ''
        print(f"   🚉 Train {train_number} assigned track {track}{loc} → Alerting {phone}")
        sms_service._send_ntfy(
            title=f"Train {train_number} - Track {track}",
            message=f"Track {track} assigned{loc}{departure_time}",
            priority="default",
            topic=ntfy_topic,
            click_url=manage_url
        )
    elif not track and prev_track:
        last_track[track_key] = ''  # track was cleared (reassignment pending)

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
        sms_service.send_delay_alert(phone, train_number, status['delay_minutes'], station_name,
                                     ntfy_topic=ntfy_topic, manage_url=manage_url)
        sent_alerts[alert_key] = _now_et()

    # Send cancellation alert
    elif status['cancelled'] and send_delay:
        print(f"   🚫 Train {train_number} cancelled → Alerting {phone}")
        sms_service.send_cancellation_alert(phone, train_number, station_name,
                                            ntfy_topic=ntfy_topic, manage_url=manage_url)
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
            sms_service.send_ontime_alert(phone, train_number, departure_time, station_name,
                                         ntfy_topic=ntfy_topic, manage_url=manage_url)
            sent_alerts[alert_key] = _now_et()

    else:
        print(f"   ℹ️  Train {train_number} status: {status['status']} (no alert needed)")

def cleanup_old_alerts():
    """Clean up old alert tracking and track assignments (runs daily)"""
    print("🧹 Cleaning up old alert tracking...")
    current_time = _now_et()

    # Remove alerts older than 24 hours
    old_keys = [
        key for key, timestamp in sent_alerts.items()
        if (current_time - timestamp).total_seconds() > 86400  # 24 hours
    ]
    for key in old_keys:
        del sent_alerts[key]

    # Reset track assignments and service alert dedup at midnight
    last_track.clear()
    sent_service_alerts.clear()

    print(f"   Removed {len(old_keys)} old alert(s), reset track + service alert state")

def main():
    """Main worker loop"""
    print("\n" + "="*60)
    print("🚂 NJ TRANSIT DELAY ALERTS - BACKGROUND WORKER")
    print("="*60)
    print(f"Started at: {_now_et().strftime('%Y-%m-%d %H:%M:%S')} ET")
    print("Checking trains every 2 minutes...")
    print("Press Ctrl+C to stop")
    print("="*60 + "\n")

    # Schedule jobs
    schedule.every(2).minutes.do(check_trains)
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
