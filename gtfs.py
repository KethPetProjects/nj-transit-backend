"""
GTFS (General Transit Feed Specification) module for NJ Transit rail data.
Downloads, parses, and stores schedule data in PostgreSQL.
Replaces getStationSchedule API (5 calls/day limit) with unlimited static GTFS data.
"""
import io
import zipfile
import csv
import requests
import threading
from datetime import datetime, date, timedelta
from typing import Optional, List
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import os

DATABASE_URL = os.getenv('DATABASE_URL')
GTFS_URL = "https://www.njtransit.com/rail_data.zip"
REFRESH_INTERVAL_DAYS = 7

# NYC-area destinations — trains headed here are "outbound" (commuter direction toward NYC)
NYC_DESTINATIONS = [
    'new york', 'ny penn', 'psny', 'penn station new york',
    'newark', 'newark penn', 'hoboken', 'jersey city', 'secaucus'
]


def get_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable not set!")
    return psycopg2.connect(DATABASE_URL)


def init_gtfs_tables():
    """
    Create GTFS tables and indexes if they don't exist.
    Safe to call concurrently — duplicate-table errors are treated as success
    because app.py and worker.py both import database.py on startup.
    """
    import psycopg2.errors

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_stops (
                stop_id TEXT PRIMARY KEY,
                stop_code TEXT,
                stop_name TEXT,
                stop_lat FLOAT,
                stop_lon FLOAT,
                njt_code TEXT
            )
        ''')
        # Add njt_code column if upgrading from an older schema
        c.execute('''
            ALTER TABLE gtfs_stops ADD COLUMN IF NOT EXISTS njt_code TEXT
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_routes (
                route_id TEXT PRIMARY KEY,
                route_short_name TEXT,
                route_long_name TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_trips (
                trip_id TEXT PRIMARY KEY,
                route_id TEXT,
                service_id TEXT,
                trip_headsign TEXT,
                block_id TEXT
            )
        ''')
        # Add block_id column if upgrading from an older schema
        c.execute('''
            ALTER TABLE gtfs_trips ADD COLUMN IF NOT EXISTS block_id TEXT
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_stop_times (
                trip_id TEXT,
                stop_id TEXT,
                stop_sequence INTEGER,
                arrival_time TEXT,
                departure_time TEXT,
                pickup_type INTEGER,
                drop_off_type INTEGER,
                PRIMARY KEY (trip_id, stop_id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
                service_id TEXT,
                date DATE,
                exception_type INTEGER,
                PRIMARY KEY (service_id, date)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS gtfs_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # Indexes for query performance
        c.execute('CREATE INDEX IF NOT EXISTS idx_gtfs_stops_code ON gtfs_stops (stop_code)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gtfs_stops_njt ON gtfs_stops (njt_code)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gtfs_trips_service ON gtfs_trips (service_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_stop ON gtfs_stop_times (stop_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_gtfs_calendar_date ON gtfs_calendar_dates (date, exception_type)')

        conn.commit()
        print("✅ GTFS tables initialized")
    except (psycopg2.errors.DuplicateTable, psycopg2.errors.UniqueViolation):
        # app.py and worker.py start concurrently in the same container and both
        # import database.py, so both call this function at the same time.
        # If another process won the race, the tables already exist — that's fine.
        conn.rollback()
        print("✅ GTFS tables already exist (concurrent init — OK)")
    except Exception as e:
        conn.rollback()
        print(f"❌ GTFS table initialization failed: {e}")
        raise
    finally:
        conn.close()


def get_last_updated() -> Optional[datetime]:
    """Get the last time GTFS data was refreshed from gtfs_metadata."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM gtfs_metadata WHERE key = 'last_updated'")
        result = c.fetchone()
        conn.close()
        if result:
            return datetime.fromisoformat(result[0])
        return None
    except Exception as e:
        print(f"⚠️ Could not read GTFS last_updated: {e}")
        return None


def _set_last_updated():
    """Record the current timestamp as the GTFS last-updated time."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO gtfs_metadata (key, value)
        VALUES ('last_updated', %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    ''', (datetime.now().isoformat(),))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Download + Parse
# ---------------------------------------------------------------------------

def download_and_load():
    """
    Download rail_data.zip from NJ Transit, parse each GTFS file, and
    upsert into PostgreSQL.  Can take 30-90 seconds on first run.
    """
    print(f"📥 Downloading GTFS data from {GTFS_URL} ...")
    try:
        response = requests.get(GTFS_URL, timeout=120)
        response.raise_for_status()
        mb = len(response.content) / 1024 / 1024
        print(f"✅ Downloaded {mb:.1f} MB")
    except Exception as e:
        print(f"❌ GTFS download failed: {e}")
        raise

    zip_data = io.BytesIO(response.content)

    with zipfile.ZipFile(zip_data, 'r') as zf:
        names = zf.namelist()
        print(f"📦 GTFS zip contents: {names}")

        if 'stops.txt' in names:
            _load_stops(zf)
        else:
            print("⚠️ stops.txt not found in zip")

        if 'routes.txt' in names:
            _load_routes(zf)
        else:
            print("⚠️ routes.txt not found in zip")

        if 'trips.txt' in names:
            _load_trips(zf)
        else:
            print("⚠️ trips.txt not found in zip")

        if 'stop_times.txt' in names:
            _load_stop_times(zf)
        else:
            print("⚠️ stop_times.txt not found in zip")

        if 'calendar_dates.txt' in names:
            _load_calendar_dates(zf)
        else:
            print("⚠️ calendar_dates.txt not found in zip")

    # Map NJT 2-char station codes onto gtfs_stops.njt_code so that
    # get_station_schedule('ED') can find Edison via njt_code, not the
    # GTFS numeric stop_code (e.g. '95038') which our API never uses.
    _map_njt_codes()

    _set_last_updated()
    print("✅ GTFS data fully loaded!")


def _load_stops(zf: zipfile.ZipFile):
    print("  📍 Loading stops...")
    rows = []
    with zf.open('stops.txt') as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
        for row in reader:
            rows.append((
                row.get('stop_id', '').strip(),
                row.get('stop_code', '').strip() or None,
                row.get('stop_name', '').strip() or None,
                _float_or_none(row.get('stop_lat', '')),
                _float_or_none(row.get('stop_lon', ''))
            ))

    conn = get_connection()
    c = conn.cursor()
    execute_values(c, '''
        INSERT INTO gtfs_stops (stop_id, stop_code, stop_name, stop_lat, stop_lon)
        VALUES %s
        ON CONFLICT (stop_id) DO UPDATE SET
            stop_code = EXCLUDED.stop_code,
            stop_name = EXCLUDED.stop_name,
            stop_lat  = EXCLUDED.stop_lat,
            stop_lon  = EXCLUDED.stop_lon
    ''', rows)
    conn.commit()
    conn.close()
    print(f"  ✅ {len(rows)} stops loaded")


def _load_routes(zf: zipfile.ZipFile):
    print("  🛤️  Loading routes...")
    rows = []
    with zf.open('routes.txt') as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
        for row in reader:
            rows.append((
                row.get('route_id', '').strip(),
                row.get('route_short_name', '').strip() or None,
                row.get('route_long_name', '').strip() or None,
            ))

    conn = get_connection()
    c = conn.cursor()
    execute_values(c, '''
        INSERT INTO gtfs_routes (route_id, route_short_name, route_long_name)
        VALUES %s
        ON CONFLICT (route_id) DO UPDATE SET
            route_short_name = EXCLUDED.route_short_name,
            route_long_name  = EXCLUDED.route_long_name
    ''', rows)
    conn.commit()
    conn.close()
    print(f"  ✅ {len(rows)} routes loaded")


def _load_trips(zf: zipfile.ZipFile):
    print("  🚂 Loading trips...")
    rows = []
    with zf.open('trips.txt') as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
        for row in reader:
            rows.append((
                row.get('trip_id', '').strip(),
                row.get('route_id', '').strip() or None,
                row.get('service_id', '').strip() or None,
                row.get('trip_headsign', '').strip() or None,
                row.get('block_id', '').strip() or None,
            ))

    conn = get_connection()
    c = conn.cursor()
    execute_values(c, '''
        INSERT INTO gtfs_trips (trip_id, route_id, service_id, trip_headsign, block_id)
        VALUES %s
        ON CONFLICT (trip_id) DO UPDATE SET
            route_id      = EXCLUDED.route_id,
            service_id    = EXCLUDED.service_id,
            trip_headsign = EXCLUDED.trip_headsign,
            block_id      = EXCLUDED.block_id
    ''', rows)
    conn.commit()
    conn.close()
    print(f"  ✅ {len(rows)} trips loaded")


def _load_stop_times(zf: zipfile.ZipFile):
    """Load stop_times.txt in batches — this is the largest file (100k+ rows)."""
    print("  ⏰ Loading stop times (large file, please wait)...")
    BATCH = 5000
    batch: list = []
    total = 0

    conn = get_connection()
    c = conn.cursor()
    # Truncate for a clean reload — avoids PK conflicts on re-import
    c.execute('TRUNCATE TABLE gtfs_stop_times')
    conn.commit()

    with zf.open('stop_times.txt') as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
        for row in reader:
            batch.append((
                row.get('trip_id', '').strip(),
                row.get('stop_id', '').strip(),
                _int_or_none(row.get('stop_sequence', '')) or 0,
                row.get('arrival_time', '').strip() or None,
                row.get('departure_time', '').strip() or None,
                _int_or_none(row.get('pickup_type', '0')) or 0,
                _int_or_none(row.get('drop_off_type', '0')) or 0,
            ))
            if len(batch) >= BATCH:
                execute_values(c, '''
                    INSERT INTO gtfs_stop_times
                        (trip_id, stop_id, stop_sequence, arrival_time,
                         departure_time, pickup_type, drop_off_type)
                    VALUES %s
                    ON CONFLICT (trip_id, stop_id) DO UPDATE SET
                        stop_sequence  = EXCLUDED.stop_sequence,
                        arrival_time   = EXCLUDED.arrival_time,
                        departure_time = EXCLUDED.departure_time,
                        pickup_type    = EXCLUDED.pickup_type,
                        drop_off_type  = EXCLUDED.drop_off_type
                ''', batch)
                conn.commit()
                total += len(batch)
                batch = []
                print(f"    ... {total} rows")

    if batch:
        execute_values(c, '''
            INSERT INTO gtfs_stop_times
                (trip_id, stop_id, stop_sequence, arrival_time,
                 departure_time, pickup_type, drop_off_type)
            VALUES %s
            ON CONFLICT (trip_id, stop_id) DO UPDATE SET
                stop_sequence  = EXCLUDED.stop_sequence,
                arrival_time   = EXCLUDED.arrival_time,
                departure_time = EXCLUDED.departure_time,
                pickup_type    = EXCLUDED.pickup_type,
                drop_off_type  = EXCLUDED.drop_off_type
        ''', batch)
        conn.commit()
        total += len(batch)

    conn.close()
    print(f"  ✅ {total} stop times loaded")


def _map_njt_codes():
    """
    Call NJT getStationList to get 2-char station codes (e.g. 'ED' for Edison),
    match them to GTFS stops by name, and store in gtfs_stops.njt_code.
    This is what makes get_station_schedule('ED') work with GTFS data.
    """
    base_url = os.getenv('NJT_API_URL', 'https://testraildata.njtransit.com/api/TrainData')
    username = os.getenv('NJT_USERNAME')
    password = os.getenv('NJT_PASSWORD')

    if not username or not password:
        print("  ⚠️ NJT credentials not set — skipping 2-char station code mapping")
        return

    # Reuse cached token if available, otherwise get a fresh one
    token = None
    try:
        from cache import cache_get, cache_set
        token = cache_get('nj_transit_token')
    except Exception:
        pass

    if not token:
        try:
            resp = requests.post(
                f"{base_url}/getToken",
                files={'username': (None, username), 'password': (None, password)},
                timeout=30
            )
            result = resp.json()
            if result.get('Authenticated') == 'True':
                token = result['UserToken']
                try:
                    cache_set('nj_transit_token', token, ttl_hours=23.5)
                except Exception:
                    pass
        except Exception as e:
            print(f"  ⚠️ Could not get NJT token for station mapping: {e}")
            return

    if not token:
        print("  ⚠️ No NJT token — skipping station code mapping")
        return

    # Call getStationList
    print("  🗺️  Fetching NJT station list to map 2-char codes...")
    try:
        resp = requests.post(
            f"{base_url}/getStationList",
            files={'token': (None, token)},
            timeout=30
        )
        resp.raise_for_status()
        station_list = resp.json()
    except Exception as e:
        print(f"  ⚠️ getStationList failed: {e} — station code mapping skipped")
        return

    if not isinstance(station_list, list) or not station_list:
        print(f"  ⚠️ Unexpected getStationList response — skipping mapping. Got: {str(station_list)[:200]}")
        return

    # Log the first entry so we know the field names
    print(f"  📋 getStationList sample entry: {station_list[0]}")

    # Build name → njt_code dict, trying several possible field names
    njt_map: dict = {}  # normalized_name → 2-char code
    for station in station_list:
        code = (
            station.get('STATION_2CHAR') or
            station.get('station2Char') or
            station.get('StationCode') or
            ''
        ).strip()
        name = (
            station.get('STATIONNAME') or
            station.get('STATION_NAME') or
            station.get('StationName') or
            ''
        ).strip().upper()

        if code and name:
            njt_map[name] = code
            # Also index without common suffixes for fuzzy matching
            for suffix in (' STATION', ' TRANSIT CENTER', ' JCT.', ' JCT', ' TERM'):
                if name.endswith(suffix):
                    njt_map[name[:-len(suffix)].strip()] = code

    print(f"  🗺️  Built NJT code map: {len(njt_map)} entries")

    def _normalize(name: str) -> str:
        """Normalize station name for matching — handle JCT./JUNCTION etc."""
        n = name.upper().strip()
        n = n.replace('JCT.', 'JUNCTION').replace(' JCT', ' JUNCTION')
        n = n.replace('.', '').strip()
        return n

    # Also build a normalized version of the NJT map for fuzzy matching
    njt_map_norm = {_normalize(k): v for k, v in njt_map.items()}

    # Load GTFS stops and match by name
    conn = get_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT stop_id, stop_name FROM gtfs_stops')
    stops = c.fetchall()

    updates = []
    for stop in stops:
        gtfs_name = (stop['stop_name'] or '').upper().strip()
        gtfs_norm = _normalize(gtfs_name)

        # Try exact, then normalized exact, then strip common suffixes
        code = (njt_map.get(gtfs_name) or
                njt_map_norm.get(gtfs_norm))
        if not code:
            for suffix in (' STATION', ' TRANSIT CENTER', ' JUNCTION', ' TERM'):
                if gtfs_norm.endswith(suffix):
                    code = njt_map_norm.get(gtfs_norm[:-len(suffix)].strip())
                    if code:
                        break
        if code:
            updates.append((code, stop['stop_id']))

    for njt_code, stop_id in updates:
        c.execute('UPDATE gtfs_stops SET njt_code = %s WHERE stop_id = %s', (njt_code, stop_id))
    conn.commit()
    conn.close()
    print(f"  ✅ Mapped {len(updates)}/{len(stops)} stops to NJT 2-char codes")


def _load_calendar_dates(zf: zipfile.ZipFile):
    print("  📅 Loading calendar dates...")
    rows = []
    with zf.open('calendar_dates.txt') as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
        for row in reader:
            date_str = row.get('date', '').strip()
            try:
                d = datetime.strptime(date_str, '%Y%m%d').date()
            except ValueError:
                continue
            rows.append((
                row.get('service_id', '').strip(),
                d,
                _int_or_none(row.get('exception_type', '')) or 1,
            ))

    conn = get_connection()
    c = conn.cursor()
    c.execute('TRUNCATE TABLE gtfs_calendar_dates')
    conn.commit()
    execute_values(c, '''
        INSERT INTO gtfs_calendar_dates (service_id, date, exception_type)
        VALUES %s
        ON CONFLICT (service_id, date) DO UPDATE SET
            exception_type = EXCLUDED.exception_type
    ''', rows)
    conn.commit()
    conn.close()
    print(f"  ✅ {len(rows)} calendar date entries loaded")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def _njt_codes_mapped() -> bool:
    """Return True if at least one stop has an njt_code populated."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM gtfs_stops WHERE njt_code IS NOT NULL')
        count = c.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def _block_ids_loaded() -> bool:
    """Return True if block_id is populated in gtfs_trips."""
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM gtfs_trips WHERE block_id IS NOT NULL')
        count = c.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def load_or_refresh():
    """
    Check whether GTFS data is missing or stale and refresh if needed.
    Also runs the NJT station-code mapping if it hasn't been done yet
    (e.g. after a schema upgrade that added the njt_code column).
    Called on app startup in a background thread (non-blocking).
    """
    try:
        last_updated = get_last_updated()
        if last_updated is None:
            print("🔄 No GTFS data in DB — performing initial load...")
            download_and_load()
        elif datetime.now() - last_updated > timedelta(days=REFRESH_INTERVAL_DAYS):
            age = (datetime.now() - last_updated).days
            print(f"🔄 GTFS data is {age} day(s) old — refreshing...")
            download_and_load()
        else:
            age_h = (datetime.now() - last_updated).total_seconds() / 3600
            print(f"✅ GTFS data is current (last updated {age_h:.1f} hours ago)")
            # Check if block_id is missing from trips (schema upgrade)
            if not _block_ids_loaded():
                print("🔄 block_id missing from trips — forcing full GTFS reload...")
                download_and_load()
            else:
                # Always re-run station code mapping on startup — it's fast
                # (one API call) and self-heals any missed stations from previous
                # runs without requiring a full GTFS re-download.
                print("🗺️  Re-running NJT station code mapping...")
                _map_njt_codes()
                print("✅ All GTFS data is current")
    except Exception as e:
        print(f"❌ GTFS load_or_refresh failed: {e}")


def load_or_refresh_background():
    """Start load_or_refresh in a daemon thread so it doesn't block API startup."""
    t = threading.Thread(target=load_or_refresh, daemon=True, name="gtfs-loader")
    t.start()
    return t


def get_station_schedule(station_code: str, query_date: Optional[date] = None) -> dict:
    """
    Return train schedule for a station using GTFS DB data.
    Format: {'outbound': [...], 'inbound': [...]}
    Each entry: {'id': trip_id, 'time': '07:45 AM', 'destination': headsign, 'line': route_id}

    Filters to trains that actually stop (pickup_type=0 AND drop_off_type=0),
    which permanently fixes the pass-through bug.
    """
    if query_date is None:
        query_date = date.today()

    try:
        conn = get_connection()
        c = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Resolve NJT 2-char station_code → stop_id via njt_code column
        #    (njt_code is populated by _map_njt_codes() during GTFS load)
        c.execute(
            'SELECT stop_id, stop_name FROM gtfs_stops WHERE njt_code = %s LIMIT 1',
            (station_code,)
        )
        stop_row = c.fetchone()
        if not stop_row:
            print(f"⚠️ Station code '{station_code}' not found in GTFS stops (njt_code column not yet mapped)")
            conn.close()
            return {'outbound': [], 'inbound': []}

        stop_id = stop_row['stop_id']
        stop_name = stop_row['stop_name']
        print(f"🔍 GTFS: {station_code} → {stop_name} (stop_id={stop_id})")

        # 2. Find service_ids active on query_date (exception_type=1 = service runs)
        c.execute('''
            SELECT service_id FROM gtfs_calendar_dates
            WHERE date = %s AND exception_type = 1
        ''', (query_date,))
        service_rows = c.fetchall()

        if not service_rows:
            print(f"⚠️ No active services found for {query_date} in GTFS — data may not cover this date")
            conn.close()
            return {'outbound': [], 'inbound': []}

        service_ids = [r['service_id'] for r in service_rows]
        print(f"🗓️  {len(service_ids)} service(s) active on {query_date}")

        # 3. Query trains that stop at this station on this date.
        #    pickup_type=0 AND drop_off_type=0 = normal stop (not pass-through).
        #    nyc_departure_time: departure time at the NYC hub stop (Penn Station,
        #    Hoboken, Secaucus, or Newark Penn) that occurred BEFORE this station.
        #    NULL means the train did not come from an NYC hub — skip it as inbound.
        #    For inbound trains we show the NYC hub departure time, not the home-station
        #    time, because commuters board at NYC and need to know when to be there.
        c.execute('''
            SELECT
                st.departure_time,
                st.arrival_time,
                st.stop_sequence,
                t.trip_id,
                t.block_id,
                t.trip_headsign,
                t.route_id,
                (
                    SELECT prior_st.departure_time
                    FROM gtfs_stop_times prior_st
                    JOIN gtfs_stops prior_s ON prior_st.stop_id = prior_s.stop_id
                    WHERE prior_st.trip_id = t.trip_id
                      AND prior_st.stop_sequence < st.stop_sequence
                      AND (
                        lower(prior_s.stop_name) LIKE '%%penn station%%'
                        OR lower(prior_s.stop_name) LIKE '%%hoboken%%'
                        OR lower(prior_s.stop_name) LIKE '%%secaucus%%'
                        OR lower(prior_s.stop_name) LIKE '%%newark penn%%'
                      )
                    ORDER BY prior_st.stop_sequence ASC
                    LIMIT 1
                ) AS nyc_departure_time
            FROM gtfs_stop_times st
            JOIN gtfs_trips t ON st.trip_id = t.trip_id
            WHERE st.stop_id = %s
              AND t.service_id = ANY(%s)
              AND st.pickup_type = 0
              AND st.drop_off_type = 0
            ORDER BY st.departure_time ASC
        ''', (stop_id, service_ids))

        trains = c.fetchall()
        conn.close()
        print(f"🚂 {len(trains)} trains found for {station_code} on {query_date}")

        # 4. Split outbound (→ NYC) / inbound (← NYC).
        #    - outbound: headsign contains a NYC destination; time = depart home station
        #    - inbound: nyc_departure_time is not NULL; time = depart NYC hub
        #      (commuters board at NYC — they need the NYC departure time, not home-station arrival)
        #    - skip: trains with no NYC connection at all (local shuttles, etc.)
        #    Deduplicate by (block_id, raw_time) — the Princeton Branch push-pull
        #    produces two trips with the same block_id stopping at the same station
        #    at the same time (one terminates there, one continues to Princeton).
        outbound: List[dict] = []
        inbound: List[dict] = []
        seen: set = set()  # (block_id, raw_time) already added

        for train in trains:
            headsign = (train['trip_headsign'] or '').lower()
            raw_time = train['departure_time'] or train['arrival_time'] or ''
            nyc_time = train['nyc_departure_time'] or ''
            train_id = train['block_id'] or train['trip_id']

            dedup_key = (train_id, raw_time)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            is_to_nyc = any(dest in headsign for dest in NYC_DESTINATIONS)

            if is_to_nyc:
                # Outbound: show departure time from the home station
                outbound.append({
                    'id': train_id,
                    'time': _format_gtfs_time(raw_time),
                    'destination': train['trip_headsign'] or '',
                    'line': train['route_id'] or '',
                    '_raw_time': raw_time,
                })
            elif nyc_time:
                # Inbound: show departure time from the NYC hub so commuters
                # know when to be at Penn Station / Hoboken, not when the train
                # arrives at their home station.
                inbound.append({
                    'id': train_id,
                    'time': _format_gtfs_time(nyc_time),
                    'destination': train['trip_headsign'] or '',
                    'line': train['route_id'] or '',
                    '_raw_time': nyc_time,  # sort by NYC departure time
                })

        # Sort so that post-midnight times (25:xx, 26:xx) appear at the end,
        # not mixed with afternoon trains after 12-hour conversion.
        def sort_key(t: dict) -> int:
            """Convert HH:MM:SS to total minutes; times ≥ 24h sort correctly as-is."""
            parts = t['_raw_time'].split(':')
            try:
                return int(parts[0]) * 60 + int(parts[1])
            except Exception:
                return 9999

        outbound.sort(key=sort_key)
        inbound.sort(key=sort_key)

        # Strip internal sort key before returning
        for t in outbound + inbound:
            t.pop('_raw_time', None)

        return {'outbound': outbound, 'inbound': inbound}

    except Exception as e:
        print(f"❌ GTFS get_station_schedule failed for {station_code}: {e}")
        return {'outbound': [], 'inbound': []}


def get_train_origin_njt_code(train_number: str, query_date: Optional[date] = None) -> Optional[str]:
    """
    Return the NJT 2-char station code for the first stop of train_number on
    query_date (defaults to today).  Uses GTFS block_id → trip → first stop →
    njt_code so that the worker can query getTrainSchedule at the correct origin
    station (e.g. NY Penn for evening trains, not Newark Penn).
    Returns None if the train is not in GTFS for that date.
    """
    if query_date is None:
        query_date = date.today()
    try:
        conn = get_connection()
        c = conn.cursor()

        c.execute(
            'SELECT service_id FROM gtfs_calendar_dates WHERE date = %s AND exception_type = 1',
            (query_date,)
        )
        service_ids = [r[0] for r in c.fetchall()]
        if not service_ids:
            conn.close()
            return None

        c.execute('''
            SELECT s.njt_code, s.stop_name
            FROM gtfs_stop_times st
            JOIN gtfs_trips t ON st.trip_id = t.trip_id
            JOIN gtfs_stops s ON st.stop_id = s.stop_id
            WHERE t.block_id = %s
              AND t.service_id = ANY(%s)
              AND s.njt_code IS NOT NULL
            ORDER BY st.stop_sequence ASC
            LIMIT 1
        ''', (train_number, service_ids))

        result = c.fetchone()
        conn.close()
        if result:
            print(f"🔍 GTFS origin for train {train_number}: {result[1]} ({result[0]})")
            return result[0]
        return None
    except Exception as e:
        print(f"⚠️ Could not get GTFS origin for train {train_number}: {e}")
        return None


def get_train_departure_at_station(train_number: str, station_code: str,
                                   query_date: Optional[date] = None) -> Optional[datetime]:
    """
    Return the scheduled departure datetime for train_number at station_code.
    Used by the worker to base the on-time alert window on the commuter's
    boarding station, not the train's origin.
    Returns None if not found.
    """
    if query_date is None:
        query_date = date.today()
    try:
        conn = get_connection()
        c = conn.cursor()

        c.execute(
            'SELECT service_id FROM gtfs_calendar_dates WHERE date = %s AND exception_type = 1',
            (query_date,)
        )
        service_ids = [r[0] for r in c.fetchall()]
        if not service_ids:
            conn.close()
            return None

        c.execute('SELECT stop_id FROM gtfs_stops WHERE njt_code = %s LIMIT 1', (station_code,))
        stop_row = c.fetchone()
        if not stop_row:
            conn.close()
            return None
        stop_id = stop_row[0]

        c.execute('''
            SELECT st.departure_time
            FROM gtfs_stop_times st
            JOIN gtfs_trips t ON st.trip_id = t.trip_id
            WHERE t.block_id = %s
              AND t.service_id = ANY(%s)
              AND st.stop_id = %s
              AND st.pickup_type = 0
            LIMIT 1
        ''', (train_number, service_ids, stop_id))

        result = c.fetchone()
        conn.close()
        if not result or not result[0]:
            return None

        parts = result[0].split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2]) if len(parts) > 2 else 0

        dep_date = query_date
        if hours >= 24:
            dep_date = query_date + timedelta(days=hours // 24)
            hours = hours % 24

        return datetime(dep_date.year, dep_date.month, dep_date.day, hours, minutes, seconds)
    except Exception as e:
        print(f"⚠️ get_train_departure_at_station({train_number}, {station_code}): {e}")
        return None


def get_status() -> dict:
    """Return GTFS data status: last updated timestamp + record counts."""
    try:
        conn = get_connection()
        c = conn.cursor()

        last_updated = get_last_updated()

        counts = {}
        for table in ('gtfs_stops', 'gtfs_routes', 'gtfs_trips',
                      'gtfs_stop_times', 'gtfs_calendar_dates'):
            c.execute(f'SELECT COUNT(*) FROM {table}')
            counts[table.replace('gtfs_', '')] = c.fetchone()[0]

        conn.close()
        return {
            'last_updated': last_updated.isoformat() if last_updated else None,
            'age_days': (datetime.now() - last_updated).days if last_updated else None,
            'records': counts,
        }
    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_gtfs_time(time_str: str) -> str:
    """
    Convert GTFS HH:MM:SS (may exceed 24h for post-midnight trips) to '07:45 AM'.
    """
    if not time_str:
        return ''
    try:
        parts = time_str.split(':')
        hours = int(parts[0]) % 24   # wrap 25:30 → 01:30
        minutes = int(parts[1])
        period = 'AM' if hours < 12 else 'PM'
        display = hours % 12 or 12
        return f"{display:02d}:{minutes:02d} {period}"
    except Exception:
        return time_str


def _float_or_none(val: str) -> Optional[float]:
    try:
        return float(val.strip()) if val and val.strip() else None
    except (ValueError, AttributeError):
        return None


def _int_or_none(val: str) -> Optional[int]:
    try:
        return int(val.strip()) if val and val.strip() else None
    except (ValueError, AttributeError):
        return None
