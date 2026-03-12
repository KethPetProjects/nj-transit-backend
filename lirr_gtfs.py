"""
LIRR GTFS Static Schedule — Port Washington Branch
Downloads MTA LIRR GTFS zip, parses Port Washington Branch, stores in PostgreSQL.
Exposes get_station_schedule() for the subscription UI and arrival checks.

Direction convention (MTA LIRR GTFS):
  direction_id=0 = outbound (away from Manhattan, toward Port Washington)
  direction_id=1 = inbound  (toward Manhattan, toward Penn Station / GCM)

App convention:
  "outbound" = morning commute = toward NYC  = LIRR direction_id=1
  "inbound"  = evening commute = from NYC   = LIRR direction_id=0
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
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv('DATABASE_URL')
LIRR_GTFS_URL = "http://web.mta.info/developers/data/lirr/google_transit.zip"
REFRESH_DAYS = 3   # LIRR updates more often than NJT (new timetables several times/year)

# Penn Station and Grand Central Madison stop names in LIRR GTFS
_NYC_TERMINAL_KEYWORDS = ['penn station', 'grand central', 'new york', 'atlantic terminal']


def get_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL)


# ─── Table init ────────────────────────────────────────────────────────────────

def init_lirr_tables():
    """Create LIRR GTFS tables if they don't exist."""
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
                direction_id     INTEGER
            )
        """)
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
        print(f"⚠️ LIRR table init error (may already exist): {e}")
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
    # Force re-download if lirr_calendar table is empty (schema migration)
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM lirr_calendar")
        count = c.fetchone()[0]
        conn.close()
        if count == 0:
            return True
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
    """Parse LIRR GTFS zip and upsert Port Washington Branch data into DB."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:

        # 1. Find Port Washington route_ids
        routes_csv = zf.read('routes.txt').decode('utf-8-sig')
        pw_route_ids = set()
        for row in csv.DictReader(io.StringIO(routes_csv)):
            name = (row.get('route_long_name') or row.get('route_short_name') or '').lower()
            if 'port washington' in name:
                pw_route_ids.add(row['route_id'])

        if not pw_route_ids:
            print("⚠️ No Port Washington route found in LIRR GTFS")
            return
        print(f"   Found Port Washington route(s): {pw_route_ids}")

        # 2. Collect Port Washington trips
        trips_csv = zf.read('trips.txt').decode('utf-8-sig')
        pw_trips: Dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(trips_csv)):
            if row.get('route_id') in pw_route_ids:
                pw_trips[row['trip_id']] = row
        print(f"   Found {len(pw_trips)} Port Washington trips")

        if not pw_trips:
            print("⚠️ No trips found for Port Washington route")
            return

        # 3. Parse all stops (we filter by usage below)
        stops_csv = zf.read('stops.txt').decode('utf-8-sig')
        all_stops: Dict[str, dict] = {r['stop_id']: r
                                       for r in csv.DictReader(io.StringIO(stops_csv))}

        # 4. Parse stop_times for Port Washington trips
        stop_times_csv = zf.read('stop_times.txt').decode('utf-8-sig')
        pw_stop_ids = set()
        pw_stop_times = []
        for row in csv.DictReader(io.StringIO(stop_times_csv)):
            if row['trip_id'] in pw_trips:
                pw_stop_ids.add(row['stop_id'])
                pw_stop_times.append(row)

        # 5. Calendar (recurring) and calendar_dates (exceptions) for affected service_ids
        pw_service_ids = {t['service_id'] for t in pw_trips.values()}

        # calendar.txt — recurring weekly schedule (primary for MTA LIRR)
        pw_calendar = []
        if 'calendar.txt' in zf.namelist():
            cal_csv = zf.read('calendar.txt').decode('utf-8-sig')
            pw_calendar = [r for r in csv.DictReader(io.StringIO(cal_csv))
                           if r['service_id'] in pw_service_ids]

        # calendar_dates.txt — exceptions (added/removed service on specific dates)
        cal_csv = zf.read('calendar_dates.txt').decode('utf-8-sig')
        pw_cal = [r for r in csv.DictReader(io.StringIO(cal_csv))
                  if r['service_id'] in pw_service_ids]

        print(f"   {len(pw_stop_ids)} stops, {len(pw_stop_times)} stop times, "
              f"{len(pw_calendar)} calendar rows, {len(pw_cal)} calendar_dates entries")

        # 6. Upsert into DB
        conn = get_connection()
        c = conn.cursor()
        try:
            # Stops
            for sid in pw_stop_ids:
                stop = all_stops.get(sid, {})
                c.execute("""
                    INSERT INTO lirr_stops (stop_id, stop_name, stop_lat, stop_lon)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (stop_id) DO UPDATE
                    SET stop_name=EXCLUDED.stop_name,
                        stop_lat=EXCLUDED.stop_lat,
                        stop_lon=EXCLUDED.stop_lon
                """, (sid, stop.get('stop_name'), _to_float(stop.get('stop_lat')),
                      _to_float(stop.get('stop_lon'))))

            # Trips
            for tid, t in pw_trips.items():
                c.execute("""
                    INSERT INTO lirr_trips (trip_id, route_id, service_id, trip_short_name, direction_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (trip_id) DO UPDATE
                    SET route_id=EXCLUDED.route_id,
                        service_id=EXCLUDED.service_id,
                        trip_short_name=EXCLUDED.trip_short_name,
                        direction_id=EXCLUDED.direction_id
                """, (tid, t['route_id'], t['service_id'],
                      t.get('trip_short_name', ''),
                      int(t.get('direction_id', 0) or 0)))

            # Stop times — delete affected trips first, then re-insert
            c.execute("DELETE FROM lirr_stop_times WHERE trip_id = ANY(%s)",
                      (list(pw_trips.keys()),))
            for st in pw_stop_times:
                c.execute("""
                    INSERT INTO lirr_stop_times
                      (trip_id, stop_id, stop_sequence, arrival_time, departure_time,
                       pickup_type, drop_off_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trip_id, stop_id) DO NOTHING
                """, (st['trip_id'], st['stop_id'],
                      int(st.get('stop_sequence', 0) or 0),
                      st.get('arrival_time', ''), st.get('departure_time', ''),
                      int(st.get('pickup_type', 0) or 0),
                      int(st.get('drop_off_type', 0) or 0)))

            # Calendar (recurring weekly service)
            c.execute("DELETE FROM lirr_calendar WHERE service_id = ANY(%s)",
                      (list(pw_service_ids),))
            for cal in pw_calendar:
                try:
                    sd = datetime.strptime(cal['start_date'], '%Y%m%d').date()
                    ed = datetime.strptime(cal['end_date'], '%Y%m%d').date()
                    c.execute("""
                        INSERT INTO lirr_calendar
                          (service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday, start_date, end_date)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (service_id) DO UPDATE
                        SET monday=EXCLUDED.monday, tuesday=EXCLUDED.tuesday,
                            wednesday=EXCLUDED.wednesday, thursday=EXCLUDED.thursday,
                            friday=EXCLUDED.friday, saturday=EXCLUDED.saturday,
                            sunday=EXCLUDED.sunday, start_date=EXCLUDED.start_date,
                            end_date=EXCLUDED.end_date
                    """, (cal['service_id'],
                          int(cal.get('monday',0)), int(cal.get('tuesday',0)),
                          int(cal.get('wednesday',0)), int(cal.get('thursday',0)),
                          int(cal.get('friday',0)), int(cal.get('saturday',0)),
                          int(cal.get('sunday',0)), sd, ed))
                except Exception:
                    pass

            # Calendar dates (exceptions)
            c.execute("DELETE FROM lirr_calendar_dates WHERE service_id = ANY(%s)",
                      (list(pw_service_ids),))
            for cal in pw_cal:
                try:
                    d = datetime.strptime(cal['date'], '%Y%m%d').date()
                    c.execute("""
                        INSERT INTO lirr_calendar_dates (service_id, date, exception_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (service_id, date) DO NOTHING
                    """, (cal['service_id'], d, int(cal['exception_type'])))
                except Exception:
                    pass

            # Metadata
            now_str = datetime.utcnow().isoformat()
            c.execute("""
                INSERT INTO lirr_metadata (key, value) VALUES ('last_updated', %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """, (now_str,))

            conn.commit()
            print(f"✅ LIRR GTFS loaded: {len(pw_stop_ids)} stops, {len(pw_trips)} trips, "
                  f"{len(pw_stop_times)} stop times")
        finally:
            conn.close()


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


# ─── Station list ─────────────────────────────────────────────────────────────

def get_port_washington_stations() -> List[Dict]:
    """
    Return Port Washington Branch stops ordered by position (Penn Station first).
    Excludes Penn Station / Grand Central — users board at suburban stops.
    Each entry: {id: stop_id, name: stop_name}
    """
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Pick a representative inbound trip (direction_id=1, toward Penn)
        # and order stops by stop_sequence ascending = Penn last = home stations first
        c.execute("""
            SELECT DISTINCT ON (s.stop_id)
                   s.stop_id, s.stop_name, st.stop_sequence
            FROM lirr_stops s
            JOIN lirr_stop_times st ON s.stop_id = st.stop_id
            JOIN lirr_trips t ON st.trip_id = t.trip_id
            WHERE t.direction_id = 1
            ORDER BY s.stop_id, st.stop_sequence DESC
        """)
        rows = c.fetchall()
        if not rows:
            return []

        # Deduplicate, then sort by stop_sequence descending (Port Washington first, Penn last)
        seen: Dict[str, dict] = {}
        for row in rows:
            sid = row['stop_id']
            if sid not in seen or row['stop_sequence'] > seen[sid]['stop_sequence']:
                seen[sid] = dict(row)

        stations = sorted(seen.values(), key=lambda x: x['stop_sequence'], reverse=True)

        # Exclude NYC terminals — subscribers board at home stations
        return [
            {'id': s['stop_id'], 'name': s['stop_name']}
            for s in stations
            if not any(kw in (s['stop_name'] or '').lower() for kw in _NYC_TERMINAL_KEYWORDS)
        ]
    except Exception as e:
        print(f"⚠️ get_port_washington_stations failed: {e}")
        return []
    finally:
        conn.close()


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
    (recurring weekly schedule) and calendar_dates.txt (exceptions).
    """
    day_col = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'][query_date.weekday()]

    # 1. Recurring service active on this day of week within date range
    c.execute(f"""
        SELECT service_id FROM lirr_calendar
        WHERE {day_col} = 1 AND start_date <= %s AND end_date >= %s
    """, (query_date, query_date))
    service_ids = {r[0] for r in c.fetchall()}

    # 2. Apply exceptions: add exception_type=1, remove exception_type=2
    c.execute("""
        SELECT service_id, exception_type FROM lirr_calendar_dates
        WHERE date = %s
    """, (query_date,))
    for row in c.fetchall():
        if row[1] == 1:
            service_ids.add(row[0])
        elif row[1] == 2:
            service_ids.discard(row[0])

    return list(service_ids)


def get_station_schedule(stop_id: str, query_date: date = None) -> dict:
    """
    Return outbound (morning, toward Penn) and inbound (evening, from Penn)
    trains for a Port Washington stop on query_date.

    Same return format as NJTransitAPI.get_station_schedule():
      {'outbound': [{id, time, destination, line}, ...],
       'inbound':  [{id, time, destination, line}, ...]}

    App direction mapping:
      outbound (to NYC, morning) = LIRR direction_id=1 trains where pickup_type=0
      inbound  (from NYC, evening) = LIRR direction_id=0 trains where drop_off_type=0
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
            time_str = _format_gtfs_time(row['departure_time'] or row['arrival_time'] or '',
                                          query_date)
            entry = {
                'id': train_num,
                'time': time_str,
                'destination': '',
                'line': 'Port Washington'
            }

            if row['direction_id'] == 1 and row['pickup_type'] == 0:
                # Toward Penn Station — morning outbound for commuter
                entry['destination'] = 'Penn Station NY'
                outbound.append(entry)
            elif row['direction_id'] == 0 and row['drop_off_type'] == 0:
                # Away from Penn Station — evening inbound for commuter
                entry['destination'] = 'Port Washington'
                inbound.append(entry)

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
    Used by the worker for the check-window calculation.
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
        return {
            'last_updated': get_last_updated(),
            'stops': stops,
            'trips': trips,
            'stop_times': stop_times,
            'branch': 'Port Washington'
        }
    except Exception as e:
        return {'error': str(e)}
    finally:
        conn.close()
