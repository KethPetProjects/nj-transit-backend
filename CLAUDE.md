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
- Cache is file-based (/tmp) — lost on container restart (low priority, token cached in DB would be better)
- Worker runs in same container as API (should be separate in production)
- No monitoring/alerting set up yet

## Important: Never Do This
- Never commit .env files
- Never hardcode credentials
- Never use sudo with npm/pip
- Never change the Dockerfile platform (must stay linux/amd64)
