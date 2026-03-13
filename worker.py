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
from cache import cache_get, cache_set
import lirr_realtime

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

# Track trains we sent a cancellation alert for — used to send reinstatement notices
cancel_alerted = set()  # f"{phone}_{train}"

# Track service alerts already sent this session (dedup)
sent_service_alerts = set()

ACTIVE_HOUR_START = 6   # 6 AM ET
ACTIVE_HOUR_END   = 20  # 8 PM ET

MORNING_START = 6   # 6 AM — morning trains start being checked
MORNING_END   = 10  # 10 AM — stop checking morning trains
EVENING_START = 14  # 2 PM — start checking evening trains
EVENING_END   = 20  # 8 PM — stop checking evening trains

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
    nj_transit.clear_cycle_cache()     # fresh NJT station data each cycle
    lirr_realtime.clear_cycle_cache()  # fresh LIRR RT feed each cycle

    try:
        subscriptions = get_active_subscriptions()
    except Exception as e:
        print(f"   ❌ DB error fetching subscriptions — skipping this cycle: {e}")
        return

    if not subscriptions:
        print("   No active subscriptions")
        return

    print(f"   Found {len(subscriptions)} active subscription(s)")

    check_service_alerts()

    for sub in subscriptions:
        try:
            check_subscriber_trains(sub)
        except Exception as e:
            print(f"   ❌ Error checking trains for {sub.get('phone','?')}: {e}")

def _format_context(context_statuses: list, station: str = '') -> str:
    """Format backup trains as summary lines for cascade notifications."""
    lines = []
    for train_num, status in context_statuses:
        sched = status.get('scheduled_departure')
        # For morning trains, look up departure at the commuter's boarding station
        if station and sched:
            try:
                import gtfs as _gtfs
                gt = _gtfs.get_train_departure_at_station(train_num, station)
                if gt:
                    sched = gt
            except Exception:
                pass
        if status['cancelled']:
            lines.append(f"  Train {train_num}: Cancelled")
        elif status['delayed']:
            actual = status.get('actual_departure')
            t = actual.strftime('%I:%M %p') if actual else (sched.strftime('%I:%M %p') if sched else '?')
            lines.append(f"  Train {train_num}: Delayed {status['delay_minutes']} min ({t})")
        elif status['on_time']:
            t = sched.strftime('%I:%M %p') if sched else '?'
            lines.append(f"  Train {train_num}: On time, {t}")
        else:
            lines.append(f"  Train {train_num}: Status unknown")
    return "\n".join(lines)


def check_subscriber_trains(sub: dict):
    """Check all trains for a specific subscriber"""
    phone = sub['phone']
    home_station = sub.get('station') or ''
    ntfy_topic = sub.get('ntfy_topic') or None
    rail_system = sub.get('rail_system') or 'NJT'
    manage_url = f"https://black-plant-0162ad510.4.azurestaticapps.net/?topic={ntfy_topic}" if ntfy_topic else None

    # Use new multi-train columns, fall back to old single-train columns
    morning_trains = sub.get('morning_trains') or ([sub['morning_train']] if sub.get('morning_train') else [])
    evening_trains = sub.get('evening_trains') or ([sub['evening_train']] if sub.get('evening_train') else [])
    morning_trains = [t for t in morning_trains if t]
    evening_trains = [t for t in evening_trains if t]

    morning_station_name = ''
    evening_station_name = ''
    try:
        if rail_system == 'LIRR':
            import lirr_gtfs as _lirr
            if home_station:
                morning_station_name = _lirr.get_station_name(home_station) or home_station
            evening_station_name = 'Penn Station NY'
        else:
            import gtfs as _gtfs
            if home_station:
                morning_station_name = _gtfs.get_station_name(home_station) or home_station
            if evening_trains:
                origin_code = _gtfs.get_train_origin_njt_code(evening_trains[0])
                if origin_code:
                    evening_station_name = _gtfs.get_station_name(origin_code) or ''
    except Exception:
        pass

    now_hour = _now_et().hour

    if morning_trains and MORNING_START <= now_hour < MORNING_END:
        check_train_group(
            phone=phone,
            trains=morning_trains,
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts'],
            station=home_station,
            station_name=morning_station_name,
            ntfy_topic=ntfy_topic,
            manage_url=manage_url,
            rail_system=rail_system
        )

    if evening_trains and EVENING_START <= now_hour < EVENING_END:
        check_train_group(
            phone=phone,
            trains=evening_trains,
            send_delay=sub['delay_alerts'],
            send_ontime=sub['ontime_alerts'],
            station='',
            station_name=evening_station_name,
            ntfy_topic=ntfy_topic,
            manage_url=manage_url,
            rail_system=rail_system
        )
        # Arrival alerts — NJT only (uses NJT getTrainSchedule API at home station)
        if home_station and rail_system == 'NJT':
            for t in evening_trains:
                check_arrival_alert(
                    phone=phone,
                    train_number=t,
                    home_station=home_station,
                    home_station_name=morning_station_name,
                    ntfy_topic=ntfy_topic,
                    manage_url=manage_url
                )


def _dep_str(scheduled) -> str:
    """Format a scheduled departure as '(9:36 AM)' for use in notification titles."""
    if not scheduled:
        return ''
    return f"({scheduled.strftime('%I:%M %p').lstrip('0')})"


def _lirr_label(train_number: str, status: dict) -> str:
    """
    Format LIRR train for notification titles: '9:34 PM Ronkonkoma'
    Falls back to '#1834' if time or line is unavailable.
    """
    sched = status.get('scheduled_departure')
    line = status.get('line') or status.get('destination') or ''
    if sched and line:
        return f"{sched.strftime('%I:%M %p').lstrip('0')} {line}"
    if sched:
        return f"{sched.strftime('%I:%M %p').lstrip('0')} #{train_number}"
    return f"#{train_number}"


def _gtfs_departure(train_number: str, station: str):
    """
    Return the GTFS scheduled departure time for a train at the given station.
    For morning trains: station is the subscriber's boarding station.
    For evening trains: station is empty — fall back to the train's GTFS origin.
    Returns None if GTFS has no data (caller should default to checking).
    """
    try:
        import gtfs as _gtfs
        if station:
            return _gtfs.get_train_departure_at_station(train_number, station)
        else:
            origin = _gtfs.get_train_origin_njt_code(train_number)
            if origin:
                return _gtfs.get_train_departure_at_station(train_number, origin)
    except Exception:
        pass
    return None


def _in_check_window(train_number: str, station: str, rail_system: str = 'NJT') -> bool:
    """
    Return True if this train should be checked right now.

    Start: 35 min before scheduled departure — ensures we capture the
           25–35 min on-time alert window.
    Stop:  90 min after scheduled departure — covers virtually all delays.
    Fallback: if GTFS has no departure time, always check (safe default).
    """
    if rail_system == 'LIRR':
        try:
            import lirr_gtfs as _lirr
            dep = _lirr.get_train_departure_today(train_number)
        except Exception:
            dep = None
    else:
        dep = _gtfs_departure(train_number, station)

    if dep is None:
        return True  # no GTFS data — don't skip
    minutes_until = (dep - _now_et()).total_seconds() / 60
    return -90 <= minutes_until <= 35


def check_train_group(phone: str, trains: list, send_delay: bool, send_ontime: bool,
                      station: str = '', station_name: str = '',
                      ntfy_topic: str = None, manage_url: str = None,
                      rail_system: str = 'NJT'):
    """
    Check up to 3 trains with cascade alerts.
    - Delay/cancel on any train includes status snapshot of the remaining trains.
    - On-time alert for each train includes remaining trains as context.
    """
    trains = [t for t in trains if t]
    if not trains:
        return

    # Filter to trains within their active check window before hitting the API.
    # Window: 35 min before departure → 90 min after (covers delays up to 90 min).
    # Trains outside this window are skipped entirely — no API call made.
    active_trains = [t for t in trains if _in_check_window(t, station, rail_system)]
    skipped = set(trains) - set(active_trains)
    for t in skipped:
        dep = _gtfs_departure(t, station)
        mins = f"{(dep - _now_et()).total_seconds()/60:.0f} min" if dep else "unknown"
        print(f"   ⏭️  Train {t}: {mins} until departure — outside check window, skipping")

    if not active_trains:
        return

    # Fetch all statuses upfront so context lines are always current.
    if rail_system == 'LIRR':
        statuses = [(t, lirr_realtime.get_train_status(t)) for t in active_trains]
    else:
        # Pass the boarding station for morning trains so track + departure time
        # reflect the user's stop, not the train's origin further down the line.
        query_st = station or None
        statuses = [(t, nj_transit.get_train_status(t, query_station=query_st)) for t in active_trains]

    for i, (train_number, status) in enumerate(statuses):
        context = statuses[i + 1:]  # remaining trains shown as backup context

        # Build train label here so it's available for both track and alert notifications
        if rail_system == 'LIRR':
            train_label = _lirr_label(train_number, status)
        else:
            train_label = f"Train {train_number} {_dep_str(status.get('scheduled_departure'))}"

        # ── Track notification ──────────────────────────────────────────────
        # Skip entirely for cancelled trains — NJT sometimes oscillates the track
        # field on cancelled trains, which would cause repeated track+cancel loops.
        track = status.get('track', '').strip()
        track_key = f"{phone}_{train_number}"
        prev_track = last_track.get(track_key, '')
        if not status['cancelled']:
            if track and track != prev_track and ntfy_topic:
                last_track[track_key] = track
                dep_str = ''
                if status.get('scheduled_departure'):
                    dep_str = f" | Departs {status['scheduled_departure'].strftime('%I:%M %p')}"
                loc = f" at {station_name}" if station_name else ''
                print(f"   🚉 Train {train_number} track {track}{loc} → Alerting {phone}")
                sms_service._send_ntfy(
                    title=f"{train_label} - Track {track}",
                    message=f"Track {track} assigned{loc}{dep_str}",
                    priority="default",
                    topic=ntfy_topic,
                    click_url=manage_url
                )
            elif not track and prev_track:
                last_track[track_key] = ''

        # ── Dedup check ─────────────────────────────────────────────────────
        alert_key = f"{phone}_{train_number}_{status['status']}"
        if alert_key in sent_alerts:
            if (_now_et() - sent_alerts[alert_key]).total_seconds() < 3600:
                continue

        ctx = ('\n' + _format_context(context, station)) if context else ''

        # ── Reinstatement alert ─────────────────────────────────────────────
        # If we previously alerted cancelled but train is now running, correct it
        cancel_key = f"{phone}_{train_number}"
        if not status['cancelled'] and cancel_key in cancel_alerted:
            cancel_alerted.discard(cancel_key)
            if ntfy_topic:
                print(f"   ✅ Train {train_number} reinstated → Alerting {phone}")
                sms_service._send_ntfy(
                    title=f"{train_label} - Now Running",
                    message=f"Previously shown as cancelled — train is back on schedule",
                    priority="high",
                    topic=ntfy_topic, click_url=manage_url
                )

        # ── Delay alert ─────────────────────────────────────────────────────
        if status['delayed'] and send_delay:
            actual = status.get('actual_departure')
            loc = f" at {station_name}" if station_name else ''
            # Title already says "Delayed X min" — body just shows the new time
            if actual:
                msg = f"Now departing {actual.strftime('%I:%M %p')}{loc}{ctx}"
            else:
                msg = f"{status['delay_minutes']} min delay{loc}{ctx}"
            print(f"   ⚠️  Train {train_number} delayed {status['delay_minutes']} min → Alerting {phone}")
            sms_service._send_ntfy(
                title=f"{train_label} - Delayed {status['delay_minutes']} min",
                message=msg, priority="high",
                topic=ntfy_topic, click_url=manage_url
            )
            sent_alerts[alert_key] = _now_et()

        # ── Cancellation alert ──────────────────────────────────────────────
        elif status['cancelled'] and send_delay:
            loc = f" at {station_name}" if station_name else ''
            msg = f"Cancelled{loc}{ctx}"
            print(f"   🚫 Train {train_number} cancelled → Alerting {phone}")
            sms_service._send_ntfy(
                title=f"{train_label} - Cancelled",
                message=msg, priority="urgent",
                topic=ntfy_topic, click_url=manage_url
            )
            cancel_alerted.add(cancel_key)
            sent_alerts[alert_key] = _now_et()

        # ── On-time alert (30-min window) ───────────────────────────────────
        elif status['on_time'] and send_ontime:
            scheduled = status['scheduled_departure']
            if station and rail_system == 'NJT':
                try:
                    import gtfs as _gtfs
                    gtfs_time = _gtfs.get_train_departure_at_station(train_number, station)
                    if gtfs_time:
                        scheduled = gtfs_time
                except Exception as e:
                    print(f"   ⚠️  GTFS lookup failed for {train_number} at {station}: {e}")

            if scheduled is None:
                print(f"   ⚠️  Train {train_number} has no scheduled time")
                continue

            minutes_until = (scheduled - _now_et()).total_seconds() / 60
            if 25 <= minutes_until <= 35:
                dep_time = scheduled.strftime('%I:%M %p')
                loc = f" at {station_name}" if station_name else ''
                msg = f"On time — departs {dep_time}{loc}{ctx}"
                print(f"   ✅ Train {train_number} on time → Alerting {phone}")
                sms_service._send_ntfy(
                    title=f"{train_label} - On Time",
                    message=msg, priority="default",
                    topic=ntfy_topic, click_url=manage_url
                )
                sent_alerts[alert_key] = _now_et()

        else:
            print(f"   ℹ️  Train {train_number} status: {status['status']} (no alert needed)")

def check_arrival_alert(phone: str, train_number: str, home_station: str,
                         home_station_name: str, ntfy_topic: str, manage_url: str):
    """
    Send an arrival notification for the first evening train ~30 min before it
    reaches the subscriber's home station.

    Uses getTrainSchedule(home_station) so SEC_LATE reflects the delay at that
    specific stop — mid-journey slowdowns are captured as the train progresses.

    Sends at most 2 alerts per train per day:
      1. Initial — when ETA first enters the ≤30 min window
      2. Revision — if ETA shifts ≥5 min after the initial alert (delay worsened
         or train recovered)
    """
    if not home_station or not ntfy_topic:
        return

    result = nj_transit.get_arrival_eta_at_station(train_number, home_station)
    if not result:
        return

    eta = result['eta']
    delay_minutes = result['delay_minutes']
    now = _now_et()
    minutes_away = (eta - now).total_seconds() / 60

    # Only act when ETA is ≤30 min and still in the future
    if not (0 < minutes_away <= 30):
        return

    eta_str = eta.strftime('%I:%M %p').lstrip('0')
    eta_min_of_day = eta.hour * 60 + eta.minute
    station_label = f" at {home_station_name}" if home_station_name else ''

    today_str = now.strftime('%Y-%m-%d')
    cache_key = f"arrival_notif_{phone}_{train_number}_{today_str}"
    state = cache_get(cache_key) or {}

    sent_count = state.get('count', 0)
    last_eta_min = state.get('last_eta_min')

    if sent_count == 0:
        # Initial arrival alert
        if delay_minutes > 0:
            title = f"Train {train_number} — Arrives {eta_str} ({delay_minutes} min late)"
            msg   = f"Arrives{station_label} at {eta_str} — {delay_minutes} min late"
        else:
            title = f"Train {train_number} — Arrives {eta_str} (on time)"
            msg   = f"Arrives{station_label} at {eta_str} — on time"

        print(f"   🏠 Arrival alert: Train {train_number} → {home_station} in ~{minutes_away:.0f} min")
        sms_service._send_ntfy(title=title, message=msg, priority="default",
                               topic=ntfy_topic, click_url=manage_url)
        cache_set(cache_key, {'count': 1, 'last_eta_min': eta_min_of_day}, ttl_hours=8)

    elif sent_count == 1 and last_eta_min is not None:
        # Revision: only if ETA shifted ≥5 min from what we told the family
        shift = abs(eta_min_of_day - last_eta_min)
        if shift >= 5:
            if delay_minutes > 0:
                title = f"Train {train_number} — Revised arrival {eta_str} ({delay_minutes} min late)"
                msg   = f"Revised: arrives{station_label} at {eta_str} — now {delay_minutes} min late"
            else:
                title = f"Train {train_number} — Back on time, arrives {eta_str}"
                msg   = f"Back on track — arrives{station_label} at {eta_str} on time"

            print(f"   🔄 Revised arrival: Train {train_number} ETA shifted {shift} min → {home_station}")
            sms_service._send_ntfy(title=title, message=msg, priority="default",
                                   topic=ntfy_topic, click_url=manage_url)
            cache_set(cache_key, {'count': 2, 'last_eta_min': eta_min_of_day}, ttl_hours=8)

    # sent_count >= 2: cap reached, no more arrival alerts today


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

def refresh_gtfs_and_notify():
    """
    Refresh GTFS data if stale, then notify subscribers whose saved train numbers
    are no longer in the new schedule. Runs every 2 days via the worker scheduler.
    """
    import gtfs as _gtfs
    print(f"\n🔄 [{_now_et().strftime('%H:%M:%S')}] Checking GTFS freshness...")
    try:
        before = _gtfs.get_last_updated()
        _gtfs.load_or_refresh()  # blocking — we need to know if a real refresh happened
        after = _gtfs.get_last_updated()
    except Exception as e:
        print(f"   ❌ GTFS refresh failed: {e}")
        return

    if not after or before == after:
        print("   ✅ GTFS already current — no subscriber check needed")
        return

    print(f"   🆕 GTFS refreshed — checking subscriber train numbers...")
    try:
        subscriptions = get_active_subscriptions()
    except Exception as e:
        print(f"   ❌ DB error fetching subscriptions: {e}")
        return

    notified = 0
    for sub in subscriptions:
        ntfy_topic = sub.get('ntfy_topic')
        morning_trains = sub.get('morning_trains') or ([sub['morning_train']] if sub.get('morning_train') else [])
        evening_trains = sub.get('evening_trains') or ([sub['evening_train']] if sub.get('evening_train') else [])
        all_trains = [t for t in morning_trains + evening_trains if t]
        if not all_trains or not ntfy_topic:
            continue
        try:
            missing = _gtfs.check_trains_in_schedule(all_trains)
        except Exception:
            continue
        if missing:
            missing_str = ', '.join(sorted(missing))
            manage_url = f"https://black-plant-0162ad510.4.azurestaticapps.net/?topic={ntfy_topic}"
            print(f"   📢 Train(s) {missing_str} missing from new schedule — notifying subscriber")
            sms_service._send_ntfy(
                title="NJ Transit Schedule Updated",
                message=(
                    f"NJT updated their schedule. Train(s) {missing_str} may have changed "
                    f"or been renumbered. Tap to review and update your selections."
                ),
                priority="high",
                topic=ntfy_topic,
                click_url=manage_url
            )
            notified += 1

    if notified:
        print(f"   ✅ Notified {notified} subscriber(s) of schedule changes")
    else:
        print(f"   ✅ All {len(subscriptions)} subscribers' train numbers are valid in new schedule")


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
    schedule.every(2).days.do(refresh_gtfs_and_notify)
    
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
