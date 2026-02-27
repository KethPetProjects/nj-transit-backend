# NJ Transit Delay Alerts — Backend

## Project Overview
A train delay notification service for NJ Transit commuters. Users subscribe via website, select their trains, and receive SMS alerts for delays/cancellations.

## Architecture
```
Frontend (Azure Static Web App) → Backend API (Azure Container App) → PostgreSQL (Supabase)
                                                                     → NJ Transit API
                                                                     → Twilio SMS (pending approval)
```

## This Repo — Backend
- **Language:** Python 3.11 + FastAPI
- **Database:** PostgreSQL via Supabase
- **SMS:** Twilio (currently MOCK mode — campaign pending A2P approval)
- **AI:** Claude Haiku via `/nova/chat` proxy endpoint
- **Containerized:** Docker (linux/amd64)
- **Deployed:** Azure Container Apps

## Partner Repo — Frontend
- **GitHub:** https://github.com/KethPetProjects/nj-transit-alerts
- **Live URL:** https://black-plant-0162ad510.4.azurestaticapps.net
- **Files:** index.html, nova.html, privacy.html, terms.html

## Key URLs
- **Backend API:** https://train-alerts-api.livelyhill-7b9b1325.westus2.azurecontainerapps.io
- **API Docs:** https://train-alerts-api.livelyhill-7b9b1325.westus2.azurecontainerapps.io/docs
- **Admin Panel:** https://train-alerts-api.livelyhill-7b9b1325.westus2.azurecontainerapps.io/admin/subscriptions

## Azure Resources
- **Container Registry:** kethnjtransitalerts-g3ddh7gmbzf3dxd3.azurecr.io
- **Container App:** train-alerts-api
- **Resource Group:** train-alerts-rg
- **Region:** West US 2

## Deploy Workflow
```bash
# Automatic! Just push to main:
git add .
git commit -m "your message"
git push

# GitHub Actions (.github/workflows/deploy.yml) handles:
# 1. Docker build (linux/amd64)
# 2. Push to ACR
# 3. az containerapp update
```

## Files
- `app.py` — FastAPI app, all API endpoints
- `database.py` — PostgreSQL operations (subscriptions, verification)
- `njtransit.py` — NJ Transit API client with caching
- `cache.py` — File-based cache (token + schedule caching)
- `worker.py` — Background job, checks trains every 5 minutes
- `notifications.py` — SMS service (mock until Twilio approved)
- `requirements.txt` — Python dependencies
- `Dockerfile` — Docker build config

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | / | Health check |
| POST | /subscribe | Subscribe to alerts |
| POST | /verify | Verify phone with code |
| GET | /subscription/{phone} | Get subscription status |
| POST | /unsubscribe | Legacy unsubscribe |
| POST | /unsubscribe/request | Step 1: Send unsub code |
| POST | /unsubscribe/confirm | Step 2: Confirm unsub |
| GET | /trains/{station_code} | Get trains for station |
| POST | /nova/chat | Claude Haiku AI proxy |
| POST | /twilio/webhook | STOP/HELP webhook |
| GET | /admin/subscriptions | List all subs (auth required) |
| DELETE | /admin/subscription/{phone} | Delete sub (auth required) |
| GET | /admin/export | Export data (auth required) |
| GET | /stats | Subscriber count |

## Environment Variables (set in Azure)
- `NJT_USERNAME` — NJ Transit API username
- `NJT_PASSWORD` — NJ Transit API password
- `DATABASE_URL` — Supabase PostgreSQL connection string
- `ADMIN_USERNAME` — Admin panel username
- `ADMIN_PASSWORD` — Admin panel password
- `ANTHROPIC_API_KEY` — For Nova AI (/nova/chat endpoint)
- `TWILIO_ACCOUNT_SID` — (add when Twilio approved)
- `TWILIO_AUTH_TOKEN` — (add when Twilio approved)
- `TWILIO_PHONE_NUMBER` — +19733142062

## NJ Transit API Rate Limits (CRITICAL)
- `getToken`: 10 calls/day — cached 23.5 hours
- `getStationSchedule`: 5 calls/day — cached 24 hours
- `getTrainSchedule`: 40,000 calls/day
- Base URL: https://testraildata.njtransit.com/api/TrainData

## Database Schema
```sql
CREATE TABLE subscriptions (
    id SERIAL PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    morning_train TEXT NOT NULL,
    evening_train TEXT NOT NULL,
    delay_alerts BOOLEAN DEFAULT TRUE,
    ontime_alerts BOOLEAN DEFAULT TRUE,
    verification_code TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Security
- Admin endpoints: HTTP Basic Auth
- Twilio webhook: Signature validation (using RequestValidator)
- No credentials in code — all in Azure env vars
- Constant-time password comparison (timing attack prevention)

## Testing
- Use `000000` as universal verification code (test mode)
- SMS is in MOCK mode — prints to logs instead of sending
- Check logs: `az containerapp logs show --name train-alerts-api --resource-group train-alerts-rg --tail 20`

## Twilio Status
- Campaign submitted for A2P 10DLC approval
- First submission rejected (error 30909 — incomplete CTA)
- Resubmitted with proper consent language
- Awaiting approval (~2-3 business days)
- When approved: add TWILIO_* env vars to Azure → SMS goes live instantly

## Known Issues / TODO
- Worker runs in same container as API (should be separate in production)
- No monitoring/alerting set up yet

## NEXT TASK — GTFS Implementation (replace getStationSchedule)
**Status: Planned, not started**

### Problem
`getStationSchedule` has a 5 calls/day total limit across ALL stations and corridors.
This is not scalable as users from multiple NJ Transit lines join.

### Solution: Use NJ Transit GTFS Static Data
NJ Transit publishes a free public GTFS zip (no auth, no rate limit):
```
https://www.njtransit.com/rail_data.zip
```
Updated by NJ Transit whenever schedules change (~seasonally).

### GTFS Files We Need
| File | Contents |
|------|----------|
| `stops.txt` | All stations: stop_id, stop_code (= 2-char code), stop_name, lat/lon |
| `trips.txt` | All trips: trip_id, route_id, service_id, headsign |
| `stop_times.txt` | Every stop for every trip: trip_id, stop_id, arrival_time, departure_time, pickup_type, drop_off_type |
| `calendar_dates.txt` | Which service_id runs on which date (handles weekday/weekend/holiday) |
| `routes.txt` | Line names and codes |

### New File: gtfs.py
Responsibilities:
1. Download `rail_data.zip` from NJ Transit (on startup + weekly refresh)
2. Parse CSV files from zip in memory
3. Upsert into PostgreSQL tables
4. Expose `get_station_schedule(station_code, date)` that queries DB

### New Database Tables
```sql
CREATE TABLE gtfs_stops (
    stop_id TEXT PRIMARY KEY,
    stop_code TEXT,          -- 2-char NJT code (e.g. 'ED', 'NP')
    stop_name TEXT,
    stop_lat FLOAT,
    stop_lon FLOAT
);

CREATE TABLE gtfs_routes (
    route_id TEXT PRIMARY KEY,
    route_short_name TEXT,   -- e.g. 'NEC', 'NJCL'
    route_long_name TEXT     -- e.g. 'Northeast Corridor'
);

CREATE TABLE gtfs_trips (
    trip_id TEXT PRIMARY KEY,
    route_id TEXT,
    service_id TEXT,
    trip_headsign TEXT
);

CREATE TABLE gtfs_stop_times (
    trip_id TEXT,
    stop_id TEXT,
    stop_sequence INTEGER,
    arrival_time TEXT,
    departure_time TEXT,
    pickup_type INTEGER,     -- 0=normal, 1=no pickup
    drop_off_type INTEGER,   -- 0=normal, 1=no dropoff
    PRIMARY KEY (trip_id, stop_id)
);

CREATE TABLE gtfs_calendar_dates (
    service_id TEXT,
    date DATE,
    exception_type INTEGER,  -- 1=service added, 2=service removed
    PRIMARY KEY (service_id, date)
);

CREATE TABLE gtfs_metadata (
    key TEXT PRIMARY KEY,
    value TEXT               -- stores last_updated timestamp
);
```

### Updated get_station_schedule Logic (in njtransit.py or gtfs.py)
```python
def get_station_schedule(station_code, date=None):
    # date defaults to today
    # 1. Find stop_id for station_code from gtfs_stops
    # 2. Find all service_ids running on date from gtfs_calendar_dates
    # 3. Join gtfs_stop_times + gtfs_trips filtered by stop_id + service_ids
    # 4. Filter pickup_type=0 (trains that actually stop, not pass-through)
    # 5. Split into outbound (to NYC) and inbound (from NYC) by headsign
    # 6. Return same format as before: {'outbound': [...], 'inbound': [...]}
```

### Startup Logic (in app.py)
```python
# On startup:
# 1. Call gtfs.load_or_refresh()
#    - Check gtfs_metadata for last_updated
#    - If never loaded OR older than 7 days: download + parse + upsert
#    - Otherwise: skip (data already in DB)
# 2. Schedule weekly refresh via background thread
```

### New Admin Endpoint
```
GET /admin/gtfs/refresh  — force re-download GTFS data (auth required)
GET /admin/gtfs/status   — show last updated timestamp + record counts
```

### Files to Modify
1. **NEW: `gtfs.py`** — download, parse, load GTFS into DB
2. **`database.py`** — add `init_gtfs_tables()` function
3. **`njtransit.py`** — replace `get_station_schedule()` to use GTFS DB instead of API
4. **`app.py`** — call `gtfs.load_or_refresh()` on startup, add admin endpoints
5. **`requirements.txt`** — no new deps needed (requests + psycopg2 already there)

### Fallback
Keep `_mock_station_trains()` as fallback if GTFS data not yet loaded.

### NJ Transit API Endpoints (full list discovered)
| Endpoint | Rate Limit | Use |
|----------|-----------|-----|
| `getToken` | 10/day | Auth — keep as-is |
| `getStationSchedule` | 5/day | REPLACE with GTFS |
| `getTrainSchedule` | 40,000/day | Real-time alerts — keep as-is |
| `getTrainStopList` | 40,000/day | Get all stops for a train by ID |
| `getStationList` | High | All station codes/names |
| `getStationMSG` | High | Station alert messages |
| `getVehicleData` | 40,000/day | GPS + delay for active trains |
| `getTrainSchedule19Rec` | 40,000/day | Same as getTrainSchedule, no stops |
| `isValidToken` | 10/day | Token validation |

### Key Insight on Pass-Through Bug
Train 3828 showed at Edison when it shouldn't. GTFS fixes this permanently:
- `pickup_type=0 AND drop_off_type=0` = normal stop (train actually stops)
- Any other value = conditional/no stop — filter these OUT

## Important: Never Do This
- Never commit .env files
- Never hardcode credentials
- Never use sudo with npm/pip
- Never change the Dockerfile platform (must stay linux/amd64)
