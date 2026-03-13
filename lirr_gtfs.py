"""
LIRR GTFS Static Schedule — Babylon, Ronkonkoma, and Port Washington branches.
Downloads MTA LIRR GTFS zip, parses Penn-serving trips for all three branches,
stores in PostgreSQL.

Direction convention (MTA LIRR GTFS):
  direction_id=0 = outbound (away from Manhattan)
  direction_id=1 = inbound  (toward Manhattan / Penn Station)

App convention:
  "outbound" = morning commute = toward Penn  = LIRR direction_id=1
  "inbound"  = evening commute = from Penn    = LIRR direction_id=0
"""
import io
import csv
import os
import threading
import zipfile
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import psycopg2
import requests
from psycopg2.extras import RealDictCursor, execute_values

DATABASE_URL = os.getenv('DATABASE_URL')
LIRR_GTFS_URL = "http://web.mta.info/developers/data/lirr/google_transit.zip"
REFRESH_DAYS = 3

# Branches we support: route_id → display name
TARGET_ROUTES: Dict[str, str] = {
    '1': 'Babylon',
    '4': 'Ronkonkoma',
    '9': 'Port Washington',
}

PENN_STOP_ID = '237'

# Stop IDs to exclude from home-station lists (NYC terminals)
EXCLUDE_HOME_STOPS = {'237', '349', '241'}  # Penn, Grand Central, Atlantic Terminal


def get_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL)


# ─── Table init ────────────────────────────────────────────────────────────────

def init_lirr_tables():
    """Create LIRR GTFS tables if they don't exist, and migrate schema."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_stops (
                stop_id   TEXT PRIMARY KEY,
                stop_name TEXT,
                stop_lat  FLOAT,
                stop_lon  FLOAT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_trips (
                trip_id          TEXT PRIMARY KEY,
                route_id         TEXT,
                service_id       TEXT,
                trip_short_name  TEXT,
                direction_id     INTEGER,
                trip_headsign    TEXT,
                route_name       TEXT
            )
        """)
        # Migrate: add new columns if they don't exist
        c.execute("ALTER TABLE lirr_trips ADD COLUMN IF NOT EXISTS trip_headsign TEXT")
        c.execute("ALTER TABLE lirr_trips ADD COLUMN IF NOT EXISTS route_name TEXT")

        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_stop_times (
                trip_id        TEXT,
                stop_id        TEXT,
                stop_sequence  INTEGER,
                arrival_time   TEXT,
                departure_time TEXT,
                pickup_type    INTEGER DEFAULT 0,
                drop_off_type  INTEGER DEFAULT 0,
                PRIMARY KEY (trip_id, stop_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_calendar (
                service_id  TEXT PRIMARY KEY,
                monday      INTEGER,
                tuesday     INTEGER,
                wednesday   INTEGER,
                thursday    INTEGER,
                friday      INTEGER,
                saturday    INTEGER,
                sunday      INTEGER,
                start_date  DATE,
                end_date    DATE
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_calendar_dates (
                service_id      TEXT,
                date            DATE,
                exception_type  INTEGER,
                PRIMARY KEY (service_id, date)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS lirr_metadata (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        print("✅ LIRR DB tables ready")
    except Exception as e:
        print(f"⚠️ LIRR table init error: {e}")
        conn.rollback()
    finally:
        conn.close()


# ─── Metadata / freshness ──────────────────────────────────────────────────────

def get_last_updated() -> Optional[str]:
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM lirr_metadata WHERE key='last_updated'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _needs_refresh() -> bool:
    last = get_last_updated()
    if not last:
        return True
    try:
        conn = get_connection()
        c = conn.cursor()
        # Force refresh if stop_times are missing
        c.execute("SELECT COUNT(*) FROM lirr_stop_times")
        if c.fetchone()[0] == 0:
            conn.close()
            return True
        # Force refresh if not all three branches are loaded
        c.execute("SELECT COUNT(DISTINCT route_id) FROM lirr_trips")
        if c.fetchone()[0] < len(TARGET_ROUTES):
            conn.close()
            return True
        # Force refresh if today has no service coverage (GTFS schedule expired)
        today = date.today()
        c.execute(
            "SELECT COUNT(*) FROM lirr_calendar_dates WHERE date = %s AND exception_type = 1",
            (today,)
        )
        if c.fetchone()[0] == 0:
            conn.close()
            print(f"⚠️  LIRR GTFS has no service_ids for today ({today}) — forcing refresh")
            return True
        conn.close()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(last)
        return (datetime.utcnow() - dt).days >= REFRESH_DAYS
    except Exception:
        return True


# ─── Download + parse ─────────────────────────────────────────────────────────

def load_or_refresh():
    """Download and load LIRR GTFS if stale or missing."""
    if not _needs_refresh():
        print("✅ LIRR GTFS already current")
        return
    print("🚇 Downloading LIRR GTFS...")
    try:
        resp = requests.get(LIRR_GTFS_URL, timeout=60)
        resp.raise_for_status()
        _parse_and_load(resp.content)
    except Exception as e:
        print(f"❌ LIRR GTFS download failed: {e}")


def load_or_refresh_background():
    """Non-blocking version — runs in a background thread."""
    t = threading.Thread(target=load_or_refresh, daemon=True, name="lirr-gtfs-refresh")
    t.start()


def _parse_and_load(zip_bytes: bytes):
    """Parse LIRR GTFS zip and upsert Babylon/Ronkonkoma/Port Washington data."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:

        # 1. Parse all stops
        stops_csv = zf.read('stops.txt').decode('utf-8-sig')
        all_stops: Dict[str, dict] = {
            r['stop_id']: r for r in csv.DictReader(io.StringIO(stops_csv))
        }

        # 2. Collect trips for all three target routes
        trips_csv = zf.read('trips.txt').decode('utf-8-sig')
        all_trips: Dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(trips_csv)):
            if row.get('route_id') in TARGET_ROUTES:
                all_trips[row['trip_id']] = row
        print(f"   Found {len(all_trips)} trips across routes {set(TARGET_ROUTES.keys())}")

        if not all_trips:
            print("⚠️ No trips found for target routes")
            return

        # 3. Parse stop_times for target trips
        stop_times_csv = zf.read('stop_times.txt').decode('utf-8-sig')
        trip_stop_ids: Dict[str, set] = {}   # trip_id → set of stop_ids
        all_stop_times: List[dict] = []
        for row in csv.DictReader(io.StringIO(stop_times_csv)):
            if row['trip_id'] in all_trips:
                if row['trip_id'] not in trip_stop_ids:
                    trip_stop_ids[row['trip_id']] = set()
                trip_stop_ids[row['trip_id']].add(row['stop_id'])
                all_stop_times.append(row)

        # 4. Keep only Penn-serving trips (Penn stop_id must be in the trip's stops)
        penn_trip_ids = {tid for tid, sids in trip_stop_ids.items() if PENN_STOP_ID in sids}
        target_trips = {tid: t for tid, t in all_trips.items() if tid in penn_trip_ids}
        target_stop_times = [st for st in all_stop_times if st['trip_id'] in target_trips]
        target_stop_ids = {st['stop_id'] for st in target_stop_times}

        print(f"   Penn-serving trips: {len(target_trips)} "
              f"({sum(1 for t in target_trips.values() if t['route_id']=='1')} Babylon, "
              f"{sum(1 for t in target_trips.values() if t['route_id']=='4')} Ronkonkoma, "
              f"{sum(1 for t in target_trips.values() if t['route_id']=='9')} Port Washington)")
        print(f"   {len(target_stop_ids)} stops, {len(target_stop_times)} stop times")

        # 5. Calendar data for affected service_ids
        target_service_ids = {t['service_id'] for t in target_trips.values()}

        target_calendar = []
        if 'calendar.txt' in zf.namelist():
            cal_csv = zf.read('calendar.txt').decode('utf-8-sig')
            target_calendar = [r for r in csv.DictReader(io.StringIO(cal_csv))
                               if r['service_id'] in target_service_ids]

        cal_dates_csv = zf.read('calendar_dates.txt').decode('utf-8-sig')
        target_cal_dates = [r for r in csv.DictReader(io.StringIO(cal_dates_csv))
                            if r['service_id'] in target_service_ids]

        print(f"   {len(target_calendar)} calendar rows, {len(target_cal_dates)} calendar_dates entries")

        # 6. Upsert into DB
        conn = get_connection()
        c = conn.cursor()
        try:
            # Stops
            stop_rows = [
                (sid,
                 all_stops.get(sid, {}).get('stop_name'),
                 _to_float(all_stops.get(sid, {}).get('stop_lat')),
                 _to_float(all_stops.get(sid, {}).get('stop_lon')))
                for sid in target_stop_ids
            ]
            execute_values(c, """
                INSERT INTO lirr_stops (stop_id, stop_name, stop_lat, stop_lon)
                VALUES %s
                ON CONFLICT (stop_id) DO UPDATE
                SET stop_name=EXCLUDED.stop_name,
                    stop_lat=EXCLUDED.stop_lat,
                    stop_lon=EXCLUDED.stop_lon
            """, stop_rows)

            # Trips (with headsign and route_name)
            trip_rows = [
                (tid,
                 t['route_id'],
                 t['service_id'],
                 t.get('trip_short_name', ''),
                 int(t.get('direction_id', 0) or 0),
                 t.get('trip_headsign', ''),
                 TARGET_ROUTES.get(t['route_id'], ''))
                for tid, t in target_trips.items()
            ]
            execute_values(c, """
                INSERT INTO lirr_trips
                  (trip_id, route_id, service_id, trip_short_name, direction_id,
                   trip_headsign, route_name)
                VALUES %s
                ON CONFLICT (trip_id) DO UPDATE
                SET route_id=EXCLUDED.route_id,
                    service_id=EXCLUDED.service_id,
                    trip_short_name=EXCLUDED.trip_short_name,
                    direction_id=EXCLUDED.direction_id,
                    trip_headsign=EXCLUDED.trip_headsign,
                    route_name=EXCLUDED.route_name
            """, trip_rows)

            # Stop times — clear old data for these trips then re-insert
            c.execute("DELETE FROM lirr_stop_times WHERE trip_id = ANY(%s)",
                      (list(target_trips.keys()),))
            # Also clear orphaned trips from old schema (different route set)
            c.execute("""
                DELETE FROM lirr_stop_times
                WHERE trip_id NOT IN (SELECT trip_id FROM lirr_trips)
            """)
            st_rows = [
                (st['trip_id'], st['stop_id'],
                 int(st.get('stop_sequence', 0) or 0),
                 st.get('arrival_time', ''), st.get('departure_time', ''),
                 int(st.get('pickup_type', 0) or 0),
                 int(st.get('drop_off_type', 0) or 0))
                for st in target_stop_times
            ]
            execute_values(c, """
                INSERT INTO lirr_stop_times
                  (trip_id, stop_id, stop_sequence, arrival_time, departure_time,
                   pickup_type, drop_off_type)
                VALUES %s
                ON CONFLICT (trip_id, stop_id) DO NOTHING
            """, st_rows)

            # Calendar (recurring)
            c.execute("DELETE FROM lirr_calendar WHERE service_id = ANY(%s)",
                      (list(target_service_ids),))
            if target_calendar:
                cal_rows = []
                for cal in target_calendar:
                    sd = datetime.strptime(cal['start_date'], '%Y%m%d').date()
                    ed = datetime.strptime(cal['end_date'], '%Y%m%d').date()
                    cal_rows.append((
                        cal['service_id'],
                        int(cal.get('monday', 0)), int(cal.get('tuesday', 0)),
                        int(cal.get('wednesday', 0)), int(cal.get('thursday', 0)),
                        int(cal.get('friday', 0)), int(cal.get('saturday', 0)),
                        int(cal.get('sunday', 0)), sd, ed
                    ))
                execute_values(c, """
                    INSERT INTO lirr_calendar
                      (service_id, monday, tuesday, wednesday, thursday, friday,
                       saturday, sunday, start_date, end_date)
                    VALUES %s
                    ON CONFLICT (service_id) DO UPDATE
                    SET monday=EXCLUDED.monday, tuesday=EXCLUDED.tuesday,
                        wednesday=EXCLUDED.wednesday, thursday=EXCLUDED.thursday,
                        friday=EXCLUDED.friday, saturday=EXCLUDED.saturday,
                        sunday=EXCLUDED.sunday, start_date=EXCLUDED.start_date,
                        end_date=EXCLUDED.end_date
                """, cal_rows)

            # Calendar dates (exceptions)
            c.execute("DELETE FROM lirr_calendar_dates WHERE service_id = ANY(%s)",
                      (list(target_service_ids),))
            if target_cal_dates:
                caldate_rows = [
                    (cal['service_id'],
                     datetime.strptime(cal['date'], '%Y%m%d').date(),
                     int(cal['exception_type']))
                    for cal in target_cal_dates
                ]
                execute_values(c, """
                    INSERT INTO lirr_calendar_dates (service_id, date, exception_type)
                    VALUES %s
                    ON CONFLICT (service_id, date) DO NOTHING
                """, caldate_rows)

            # Metadata
            now_str = datetime.utcnow().isoformat()
            c.execute("""
                INSERT INTO lirr_metadata (key, value) VALUES ('last_updated', %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """, (now_str,))

            conn.commit()
            print(f"✅ LIRR GTFS loaded: {len(target_stop_ids)} stops, "
                  f"{len(target_trips)} trips, {len(target_stop_times)} stop times")
        except Exception as e:
            print(f"❌ LIRR DB upsert failed: {e}")
            import traceback
            traceback.print_exc()
            conn.rollback()
            raise
        finally:
            conn.close()


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


# ─── Station lists ─────────────────────────────────────────────────────────────

def get_branch_stations() -> Dict[str, List[Dict]]:
    """
    Return stations grouped by branch, ordered from home end toward Penn.
    Excludes NYC terminals (Penn, Grand Central, Atlantic Terminal).
    Returns: {'Babylon': [{id, name}, ...], 'Ronkonkoma': [...], 'Port Washington': [...]}
    """
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    try:
        result = {}
        for route_id, branch_name in TARGET_ROUTES.items():
            # For each branch: get stops from direction_id=1 (toward Penn) trips,
            # order by stop_sequence DESC so branch origin (home end) comes first.
            c.execute("""
                SELECT DISTINCT ON (s.stop_id)
                       s.stop_id, s.stop_name, st.stop_sequence
                FROM lirr_stops s
                JOIN lirr_stop_times st ON s.stop_id = st.stop_id
                JOIN lirr_trips t ON st.trip_id = t.trip_id
                WHERE t.route_id = %s AND t.direction_id = 1
                ORDER BY s.stop_id, st.stop_sequence DESC
            """, (route_id,))
            rows = c.fetchall()
            if not rows:
                result[branch_name] = []
                continue

            # Deduplicate: keep max stop_sequence per stop (= most distant from Penn)
            seen: Dict[str, dict] = {}
            for row in rows:
                sid = row['stop_id']
                if sid not in seen or row['stop_sequence'] > seen[sid]['stop_sequence']:
                    seen[sid] = dict(row)

            # Sort: lowest stop_sequence first = branch terminus first (stop_seq=1), Penn last
            stations = sorted(seen.values(), key=lambda x: x['stop_sequence'])

            result[branch_name] = [
                {'id': s['stop_id'], 'name': s['stop_name']}
                for s in stations
                if s['stop_id'] not in EXCLUDE_HOME_STOPS
            ]

        return result
    except Exception as e:
        print(f"⚠️ get_branch_stations failed: {e}")
        return {}
    finally:
        conn.close()


def get_port_washington_stations() -> List[Dict]:
    """Legacy: return Port Washington stations as flat list."""
    branches = get_branch_stations()
    return branches.get('Port Washington', [])


def get_station_name(stop_id: str) -> Optional[str]:
    """Resolve a LIRR stop_id to its display name."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT stop_name FROM lirr_stops WHERE stop_id=%s", (stop_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─── Station schedule ─────────────────────────────────────────────────────────

def _get_service_ids_for_date(c, query_date: date) -> list:
    """
    Return active service_ids for query_date by combining calendar.txt
    (recurring weekly) and calendar_dates.txt (exceptions).
    """
    day_col = ['monday', 'tuesday', 'wednesday', 'thursday',
               'friday', 'saturday', 'sunday'][query_date.weekday()]

    c.execute(f"""
        SELECT service_id FROM lirr_calendar
        WHERE {day_col} = 1 AND start_date <= %s AND end_date >= %s
    """, (query_date, query_date))
    service_ids = {r['service_id'] for r in c.fetchall()}

    c.execute("""
        SELECT service_id, exception_type FROM lirr_calendar_dates
        WHERE date = %s
    """, (query_date,))
    for row in c.fetchall():
        if row['exception_type'] == 1:
            service_ids.add(row['service_id'])
        elif row['exception_type'] == 2:
            service_ids.discard(row['service_id'])

    return list(service_ids)


def get_station_schedule(stop_id: str, query_date: date = None) -> dict:
    """
    Return outbound (morning, toward Penn) and inbound (evening, from Penn)
    trains for a given LIRR stop on query_date.

    Return format:
      {'outbound': [{id, time, destination, line}, ...],
       'inbound':  [{id, time, destination, line}, ...]}
    """
    if query_date is None:
        query_date = date.today()

    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    try:
        service_ids = _get_service_ids_for_date(c, query_date)
        if not service_ids:
            return {'outbound': [], 'inbound': []}

        c.execute("""
            SELECT t.trip_id, t.trip_short_name, t.direction_id,
                   t.trip_headsign, t.route_name,
                   st.departure_time, st.arrival_time,
                   st.pickup_type, st.drop_off_type, st.stop_sequence
            FROM lirr_trips t
            JOIN lirr_stop_times st ON t.trip_id = st.trip_id
            WHERE t.service_id = ANY(%s) AND st.stop_id = %s
            ORDER BY st.departure_time
        """, (service_ids, stop_id))
        rows = c.fetchall()

        outbound = []
        inbound = []

        for row in rows:
            train_num = row['trip_short_name'] or row['trip_id'].split('_')[-1]
            time_str = _format_gtfs_time(
                row['departure_time'] or row['arrival_time'] or '', query_date)
            line = row['route_name'] or 'LIRR'

            if row['direction_id'] == 1 and row['pickup_type'] == 0:
                outbound.append({
                    'id': train_num,
                    'time': time_str,
                    'destination': 'Penn Station NY',
                    'line': line,
                })
            elif row['direction_id'] == 0 and row['drop_off_type'] == 0:
                dest = row['trip_headsign'] or line
                inbound.append({
                    'id': train_num,
                    'time': time_str,
                    'destination': dest,
                    'line': line,
                })

        return {'outbound': outbound, 'inbound': inbound}

    except Exception as e:
        print(f"⚠️ LIRR get_station_schedule failed for {stop_id}: {e}")
        return {'outbound': [], 'inbound': []}
    finally:
        conn.close()


def _format_gtfs_time(time_str: str, ref_date: date) -> str:
    """Convert GTFS HH:MM:SS (may exceed 24h) to display time like '7:05 AM'."""
    try:
        parts = time_str.split(':')
        h, m = int(parts[0]) % 24, int(parts[1])
        dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=h, minute=m)
        return dt.strftime('%I:%M %p').lstrip('0')
    except Exception:
        return time_str[:5] if len(time_str) >= 5 else time_str


# ─── Worker helpers ───────────────────────────────────────────────────────────

def get_train_departure_today(train_number: str) -> Optional[datetime]:
    """
    Return today's scheduled first departure time for a LIRR train.
    Matches by trip_short_name OR last underscore-segment of trip_id.
    """
    today = date.today()
    conn = get_connection()
    c = conn.cursor()
    try:
        service_ids = _get_service_ids_for_date(c, today)
        if not service_ids:
            return None

        c.execute("""
            SELECT st.departure_time
            FROM lirr_trips t
            JOIN lirr_stop_times st ON t.trip_id = st.trip_id
            WHERE t.service_id = ANY(%s)
              AND (t.trip_short_name = %s OR t.trip_id LIKE %s)
            ORDER BY st.stop_sequence ASC
            LIMIT 1
        """, (service_ids, train_number, f'%_{train_number}'))
        row = c.fetchone()
        if not row or not row[0]:
            return None

        parts = row[0].split(':')
        h, m = int(parts[0]) % 24, int(parts[1])
        return datetime.combine(today, datetime.min.time()).replace(hour=h, minute=m)
    except Exception as e:
        print(f"⚠️ LIRR get_train_departure_today failed for {train_number}: {e}")
        return None
    finally:
        conn.close()


# ─── Admin status ─────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Return LIRR GTFS status (for admin endpoint)."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) FROM lirr_stops")
        stops = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM lirr_trips")
        trips = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM lirr_stop_times")
        stop_times = c.fetchone()[0]
        c.execute("SELECT route_name, COUNT(*) FROM lirr_trips GROUP BY route_name")
        by_branch = {row[0]: row[1] for row in c.fetchall()}
        return {
            'last_updated': get_last_updated(),
            'stops': stops,
            'trips': trips,
            'stop_times': stop_times,
            'branches': by_branch,
        }
    except Exception as e:
        return {'error': str(e)}
    finally:
        conn.close()
