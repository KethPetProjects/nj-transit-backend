"""
Background Worker - Checks trains every 5 minutes
Sends alerts for delays and on-time confirmations
"""
import schedule
import time
from datetime import datetime, timedelta
from database import get_active_subscriptions
from njtransit import NJTransitAPI
from notifications import SMSService

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
    print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] Checking trains...")
    
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
    
    # Check morning train
    if sub['morning_train']:
        check_train(
            phone=phone,
            train_number=sub['morning_train'],
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts']
        )
    
    # Check evening train
    if sub['evening_train']:
        check_train(
            phone=phone,
            train_number=sub['evening_train'],
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts']
        )

def check_train(phone: str, train_number: str, send_delay: bool, send_ontime: bool):
    """Check status of a specific train and send alerts if needed"""
    
    # Get train status from NJ Transit API
    status = nj_transit.get_train_status(train_number)
    
    # Create unique key to track if we've already alerted
    alert_key = f"{phone}_{train_number}_{status['status']}"
    
    # Check if we already sent this alert today
    if alert_key in sent_alerts:
        last_sent = sent_alerts[alert_key]
        if (datetime.now() - last_sent).total_seconds() < 3600:  # Don't repeat within 1 hour
            return
    
    # Send delay alert
    if status['delayed'] and send_delay:
        print(f"   ⚠️  Train {train_number} delayed {status['delay_minutes']} min → Alerting {phone}")
        sms_service.send_delay_alert(phone, train_number, status['delay_minutes'])
        sent_alerts[alert_key] = datetime.now()
    
    # Send cancellation alert
    elif status['cancelled'] and send_delay:
        print(f"   🚫 Train {train_number} cancelled → Alerting {phone}")
        sms_service.send_cancellation_alert(phone, train_number)
        sent_alerts[alert_key] = datetime.now()
    
    # Send on-time alert (30 min before departure)
    elif status['on_time'] and send_ontime:
        scheduled = status['scheduled_departure']
        
        # Check if scheduled time is available
        if scheduled is None:
            print(f"   ⚠️  Train {train_number} has no scheduled time")
            return
        
        minutes_until = (scheduled - datetime.now()).total_seconds() / 60
        
        # Send alert if train departs in 25-35 minutes (catches the 30-min window)
        if 25 <= minutes_until <= 35:
            departure_time = scheduled.strftime('%I:%M %p')
            print(f"   ✅ Train {train_number} on time → Alerting {phone}")
            sms_service.send_ontime_alert(phone, train_number, departure_time)
            sent_alerts[alert_key] = datetime.now()
    
    else:
        print(f"   ℹ️  Train {train_number} status: {status['status']} (no alert needed)")

def cleanup_old_alerts():
    """Clean up old alert tracking (runs daily)"""
    print("🧹 Cleaning up old alert tracking...")
    current_time = datetime.now()
    
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
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
