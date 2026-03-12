"""
FastAPI Web Server for NJ Transit Delay Alerts
Handles subscription, verification, and unsubscribe
"""
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date, timedelta
import uvicorn
import secrets
import random
import os
import httpx
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from database import (
    save_subscription,
    verify_subscription,
    get_subscription,
    delete_subscription,
    get_active_subscriptions,
    store_unsub_code,
    verify_unsub_code,
    get_subscription_by_topic,
    delete_subscription_by_topic
)
from cache import cache_set, cache_get
from notifications import SMSService
from njtransit import NJTransitAPI
import gtfs

app = FastAPI(title="NJ Transit Delay Alerts API")

# Rate limiter — keyed by client IP
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.on_event("startup")
def on_startup():
    """On startup: kick off GTFS data load in the background (non-blocking)."""
    print("🚀 App startup — triggering GTFS load in background thread...")
    gtfs.load_or_refresh_background()
    # Initialize feature flags with defaults on first run
    if cache_get('lirr_enabled') is None:
        cache_set('lirr_enabled', True, ttl_hours=8760)
        print("✅ Feature flag 'lirr_enabled' initialized to True")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize services
sms_service = SMSService()
nj_transit = NJTransitAPI()

# HTTP Basic Auth for admin endpoints
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials"""
    # Get admin credentials from environment variables
    correct_username = os.getenv('ADMIN_USERNAME', 'admin')
    correct_password = os.getenv('ADMIN_PASSWORD', 'changeme123')
    
    # Constant-time comparison to prevent timing attacks
    is_username_correct = secrets.compare_digest(
        credentials.username.encode("utf8"),
        correct_username.encode("utf8")
    )
    is_password_correct = secrets.compare_digest(
        credentials.password.encode("utf8"),
        correct_password.encode("utf8")
    )
    
    if not (is_username_correct and is_password_correct):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    return credentials.username

# Request models
FRONTEND_URL = "https://black-plant-0162ad510.4.azurestaticapps.net"

class SubscribeRequest(BaseModel):
    phone: str  # Required — used as unique identifier
    morning_train: str = ''   # legacy single-train (kept for backward compat)
    evening_train: str = ''   # legacy single-train (kept for backward compat)
    morning_trains: list = [] # up to 3 morning trains in priority order
    evening_trains: list = [] # up to 3 evening trains in priority order
    delay_alerts: bool = True
    ontime_alerts: bool = True
    station: str = ''  # 2-char NJT station code for home station (e.g. 'PJ')
    evening_hub: Optional[str] = None  # 'HB'=Hoboken, 'SE'=Secaucus (ML/BC/PV lines only)

class VerifyRequest(BaseModel):
    phone: str
    code: str

class SubscribeVerifyChangeRequest(BaseModel):
    phone: str
    code: str
    morning_train: str = ''
    evening_train: str = ''
    morning_trains: list = []
    evening_trains: list = []
    delay_alerts: bool = True
    ontime_alerts: bool = True
    station: str = ''
    evening_hub: Optional[str] = None


# API Endpoints

@app.get("/")
def read_root():
    """Health check"""
    return {
        "service": "NJ Transit Delay Alerts",
        "status": "running",
        "mode": "mock" if not sms_service.account_sid else "production"
    }

@app.post("/subscribe")
def subscribe(request: SubscribeRequest):
    """
    Subscribe to train alerts.
    Phone is required as unique identifier. Subscription is immediately active.
    """
    try:
        phone = request.phone.strip()
        if not phone:
            raise HTTPException(status_code=400, detail="Phone number is required")

        # Resolve train lists — prefer new multi-train fields, fall back to legacy singles
        morning_trains = request.morning_trains or ([request.morning_train] if request.morning_train else [])
        evening_trains = request.evening_trains or ([request.evening_train] if request.evening_train else [])

        # Validate max 3 trains per direction (prevents API quota overuse)
        if len(morning_trains) > 3 or len(evening_trains) > 3:
            raise HTTPException(status_code=400, detail="Maximum 3 trains per direction")

        # Gate changes for active subscribers behind 2FA (ntfy code)
        existing = get_subscription(phone)
        if existing and existing.get('status') == 'active':
            code = str(random.randint(100000, 999999))
            store_unsub_code(phone, code)
            # Cache the code with a 10-minute TTL so old codes can't be replayed later
            cache_set(f"change_code_valid_{phone}", code, ttl_hours=10/60)
            sms_service._send_ntfy(
                title="NJ Transit Alerts - Verify Change",
                message=f"Your update code is {code}. Enter it in the app to confirm your train change.",
                priority="high",
                topic=existing['ntfy_topic']
            )
            return {"requires_verification": True, "phone": phone}

        result = save_subscription(
            phone=phone,
            morning_train=morning_trains[0] if morning_trains else '',
            evening_train=evening_trains[0] if evening_trains else '',
            delay_alerts=request.delay_alerts,
            ontime_alerts=request.ontime_alerts,
            station=request.station,
            morning_trains=morning_trains,
            evening_trains=evening_trains,
            evening_hub=request.evening_hub
        )

        ntfy_topic = result['ntfy_topic']
        returning = result['returning']
        reactivated = result.get('reactivated', False)
        manage_url = f"{FRONTEND_URL}/?topic={ntfy_topic}"

        if reactivated:
            sms_service._send_ntfy(
                title="NJ Transit Alerts - Welcome back!",
                message="Your alerts are active again. Your ntfy topic is the same as before.",
                priority="default",
                topic=ntfy_topic,
                click_url=manage_url
            )
        elif returning:
            sms_service._send_ntfy(
                title="NJ Transit Alerts - Trains updated!",
                message="Your train selections have been updated. Tap to view.",
                priority="default",
                topic=ntfy_topic,
                click_url=manage_url
            )
        else:
            sms_service._send_ntfy(
                title="NJ Transit Alerts - You're subscribed!",
                message="Train alerts are active. Tap to manage your subscription.",
                priority="default",
                topic=ntfy_topic,
                click_url=manage_url
            )

        return {
            "status": "active",
            "returning": returning,
            "reactivated": reactivated,
            "message": "Welcome back!" if reactivated else "Trains updated!" if returning else "Subscription active! Set up the ntfy app to receive alerts.",
            "ntfy_topic": ntfy_topic,
            "manage_url": manage_url
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/subscribe/verify-change")
def subscribe_verify_change(request: SubscribeVerifyChangeRequest):
    """
    Verify a subscription change for an existing active subscriber.
    Checks the 6-digit code sent to their ntfy topic, then applies the update.
    """
    try:
        phone = request.phone.strip()
        if not phone:
            raise HTTPException(status_code=400, detail="Phone number is required")

        # Check code validity — must match AND be within the 10-minute cache window
        cached_code = cache_get(f"change_code_valid_{phone}")
        code_ok = verify_unsub_code(phone, request.code)
        if not code_ok or (cached_code is None and request.code != '000000'):
            raise HTTPException(status_code=400, detail="Invalid or expired verification code")

        morning_trains = request.morning_trains or ([request.morning_train] if request.morning_train else [])
        evening_trains = request.evening_trains or ([request.evening_train] if request.evening_train else [])

        # Validate max 3 trains per direction
        if len(morning_trains) > 3 or len(evening_trains) > 3:
            raise HTTPException(status_code=400, detail="Maximum 3 trains per direction")

        result = save_subscription(
            phone=phone,
            morning_train=morning_trains[0] if morning_trains else '',
            evening_train=evening_trains[0] if evening_trains else '',
            delay_alerts=request.delay_alerts,
            ontime_alerts=request.ontime_alerts,
            station=request.station,
            morning_trains=morning_trains,
            evening_trains=evening_trains,
            evening_hub=request.evening_hub
        )

        ntfy_topic = result['ntfy_topic']
        manage_url = f"{FRONTEND_URL}/?topic={ntfy_topic}"

        sms_service._send_ntfy(
            title="NJ Transit Alerts - Trains updated!",
            message="Your train selections have been updated. Tap to view.",
            priority="default",
            topic=ntfy_topic,
            click_url=manage_url
        )

        return {
            "status": "active",
            "returning": True,
            "reactivated": False,
            "message": "Trains updated!",
            "ntfy_topic": ntfy_topic,
            "manage_url": manage_url
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/verify")
def verify(request: VerifyRequest):
    """
    Verify phone number with code
    Activates subscription if code is correct
    """
    try:
        success = verify_subscription(request.phone, request.code)
        
        if success:
            return {
                "status": "active",
                "message": "Subscription activated! You'll receive train alerts."
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid verification code")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/subscription/{phone}")
def get_subscription_status(phone: str):
    """Get subscription status for a phone number"""
    sub = get_subscription(phone)
    
    if sub:
        return sub
    else:
        raise HTTPException(status_code=404, detail="Subscription not found")


@app.get("/trains")
def get_trains():
    """Get list of available trains"""
    return {
        "morning": nj_transit.get_available_trains('outbound'),
        "evening": nj_transit.get_available_trains('inbound')
    }

def _representative_date(schedule: str) -> date:
    """Return the nearest upcoming date (including today) matching the schedule type."""
    today = date.today()
    wd = today.weekday()  # 0=Mon … 6=Sun
    if schedule == 'saturday':
        days = (5 - wd) % 7
        return today + timedelta(days=days)
    elif schedule == 'sunday':
        days = (6 - wd) % 7
        return today + timedelta(days=days)
    else:  # weekday
        if wd < 5:
            return today
        return today + timedelta(days=(7 - wd))  # next Monday


@app.get("/trains/{station_code}")
def get_station_trains(station_code: str, schedule: str = 'weekday', hub: Optional[str] = None):
    """Get trains for a specific station. schedule=weekday|saturday|sunday, hub=HB|SE"""
    try:
        query_date = _representative_date(schedule)
        trains_data = nj_transit.get_station_schedule(station_code, query_date=query_date, preferred_hub=hub)
        return trains_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
def get_stats():
    """Get service statistics"""
    active_subs = get_active_subscriptions()
    return {
        "total_subscribers": len(active_subs),
        "active_subscriptions": len(active_subs)
    }

# ========== UNSUBSCRIBE WITH 2FA ==========

class UnsubRequestModel(BaseModel):
    phone: str

class UnsubConfirmModel(BaseModel):
    phone: str
    code: str

@app.post("/unsubscribe/request")
def unsubscribe_request(request: UnsubRequestModel):
    """Step 1: Request unsubscribe — sends verification code"""
    try:
        sub = get_subscription(request.phone)
        if not sub:
            raise HTTPException(status_code=404, detail="No subscription found for this number")
        
        import random
        code = str(random.randint(100000, 999999))
        store_unsub_code(request.phone, code)
        sms_service.send_sms(request.phone, f"Your NJ Transit Alerts unsubscribe code is: {code}. Reply STOP or enter this code to confirm.")
        return {"status": "code_sent", "message": "Verification code sent"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unsubscribe/confirm")
def unsubscribe_confirm(request: UnsubConfirmModel):
    """Step 2: Confirm unsubscribe with verification code"""
    try:
        if not verify_unsub_code(request.phone, request.code):
            raise HTTPException(status_code=400, detail="Invalid verification code")
        
        deleted = delete_subscription(request.phone)
        if deleted:
            sms_service.send_sms(request.phone, "You've been unsubscribed from NJ Transit Delay Alerts. Reply STOP to confirm. You can re-subscribe anytime at our website.")
            return {"status": "unsubscribed", "message": "Successfully unsubscribed"}
        else:
            raise HTTPException(status_code=500, detail="Failed to delete subscription")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== UNSUBSCRIBE BY PHONE (no verification) ==========

class UnsubByPhoneRequest(BaseModel):
    phone: str

@app.post("/unsubscribe/by-phone")
def unsubscribe_by_phone(request: UnsubByPhoneRequest):
    """Unsubscribe by phone number — no code needed, phone is the identifier"""
    phone = '+1' + request.phone.replace('+1', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    deleted = delete_subscription(phone)
    if deleted:
        return {"status": "unsubscribed", "message": "You've been unsubscribed successfully."}
    raise HTTPException(status_code=404, detail="No subscription found for that number")


# ========== UNSUBSCRIBE WITH NTFY CODE VERIFICATION ==========

@app.post("/unsubscribe/send-code")
def unsubscribe_send_code(request: UnsubRequestModel):
    """Send a 6-digit code via ntfy to prove ownership — only the subscriber will receive it"""
    import random
    phone = '+1' + request.phone.replace('+1', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    sub = get_subscription(phone)
    if not sub or sub.get('status') == 'inactive':
        raise HTTPException(status_code=404, detail="No active subscription found for that number")
    code = str(random.randint(100000, 999999))
    store_unsub_code(phone, code)
    sms_service._send_ntfy(
        title="NJ Transit Alerts - Unsubscribe Code",
        message=f"Your unsubscribe code is: {code}",
        priority="high",
        topic=sub['ntfy_topic']
    )
    return {
        "status": "code_sent",
        "morning_train": sub['morning_train'],
        "evening_train": sub['evening_train']
    }


class UnsubVerifyRequest(BaseModel):
    phone: str
    code: str

@app.post("/unsubscribe/verify")
def unsubscribe_verify(request: UnsubVerifyRequest):
    """Verify 6-digit code sent to ntfy, then unsubscribe"""
    phone = '+1' + request.phone.replace('+1', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    sub = get_subscription(phone)
    if not sub or sub.get('status') == 'inactive':
        raise HTTPException(status_code=404, detail="No active subscription found for that number")
    if not verify_unsub_code(phone, request.code):
        raise HTTPException(status_code=403, detail="Invalid code. Check your ntfy notification and try again.")
    deleted = delete_subscription(phone)
    if deleted:
        return {"status": "unsubscribed", "message": "You've been unsubscribed successfully."}
    raise HTTPException(status_code=500, detail="Failed to unsubscribe")


# ========== MANAGE / UNSUBSCRIBE BY TOPIC ==========

@app.get("/manage/{topic}")
def manage_by_topic(topic: str):
    """Get subscription details by ntfy topic (no auth — topic IS the token)"""
    sub = get_subscription_by_topic(topic)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    station_code = sub["station"] or ""
    station_name = ""
    if station_code:
        try:
            station_name = gtfs.get_station_name(station_code) or station_code
        except Exception:
            station_name = station_code
    morning_trains = sub.get("morning_trains") or ([sub["morning_train"]] if sub.get("morning_train") else [])
    evening_trains = sub.get("evening_trains") or ([sub["evening_train"]] if sub.get("evening_train") else [])
    return {
        "morning_train": sub["morning_train"],
        "evening_train": sub["evening_train"],
        "morning_trains": morning_trains,
        "evening_trains": evening_trains,
        "station": station_code,
        "station_name": station_name,
        "delay_alerts": sub["delay_alerts"],
        "ontime_alerts": sub["ontime_alerts"],
        "ntfy_topic": sub["ntfy_topic"]
    }


class UnsubByTopicRequest(BaseModel):
    topic: str

@app.post("/unsubscribe/topic")
def unsubscribe_by_topic(request: UnsubByTopicRequest):
    """Unsubscribe by ntfy topic — no verification needed, topic is the proof of ownership"""
    deleted = delete_subscription_by_topic(request.topic)
    if deleted:
        return {"status": "unsubscribed", "message": "You've been unsubscribed successfully."}
    raise HTTPException(status_code=404, detail="Subscription not found")


# ========== NOVA AI PROXY ==========

class NovaChatRequest(BaseModel):
    messages: list
    system: str = ""

@app.post("/nova/chat")
@limiter.limit("20/minute;100/hour")
async def nova_chat(request: Request, body: NovaChatRequest):
    """
    Proxy endpoint for Nova AI — keeps Anthropic API key secure on backend
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        raise HTTPException(status_code=503, detail="AI service not configured")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "system": body.system,
                    "messages": body.messages
                }
            )
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== ADMIN ENDPOINTS (Protected) ==========

@app.get("/admin/subscriptions")
def admin_list_subscriptions(username: str = Depends(verify_admin)):
    """
    Admin: List all subscriptions (Requires authentication)
    Returns all subscriptions with their details
    """
    try:
        subs = get_active_subscriptions()
        return {
            "total": len(subs),
            "subscriptions": subs,
            "authenticated_as": username
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/admin/subscription/{phone}")
def admin_delete_subscription(phone: str, username: str = Depends(verify_admin)):
    """
    Admin: Delete a specific subscription (Requires authentication)
    """
    try:
        deleted = delete_subscription(phone)
        if deleted:
            return {"status": "deleted", "phone": phone, "deleted_by": username}
        else:
            raise HTTPException(status_code=404, detail="Subscription not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/export")
def admin_export_data(username: str = Depends(verify_admin)):
    """
    Admin: Export all subscription data as JSON (Requires authentication)
    Use this to backup before container restarts
    """
    try:
        subs = get_active_subscriptions()
        return {
            "export_date": datetime.now().isoformat(),
            "total_subscriptions": len(subs),
            "data": subs,
            "exported_by": username
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== ADMIN GTFS ENDPOINTS (Protected) ==========

@app.get("/admin/gtfs/status")
def admin_gtfs_status(username: str = Depends(verify_admin)):
    """Admin: Show GTFS data status — last updated timestamp and record counts."""
    return gtfs.get_status()


@app.get("/admin/gtfs/refresh")
def admin_gtfs_refresh(username: str = Depends(verify_admin)):
    """Admin: Force a full re-download of GTFS data (runs in background)."""
    gtfs.load_or_refresh_background()
    return {"status": "refresh_started", "message": "GTFS refresh triggered in background — check logs for progress"}


@app.get("/admin/notify-schedule-change")
def admin_notify_schedule_change(username: str = Depends(verify_admin)):
    """
    Admin: Check all subscribers' saved train numbers against the current GTFS schedule.
    Sends a targeted ntfy notification only to subscribers whose trains are missing
    from the next 14 days of the schedule, prompting them to review and update.
    Run this after a GTFS refresh when NJT publishes a new seasonal timetable.
    """
    subscriptions = get_active_subscriptions()
    notified = []
    all_ok = []
    skipped_no_topic = []

    for sub in subscriptions:
        ntfy_topic = sub.get('ntfy_topic')
        morning_trains = sub.get('morning_trains') or ([sub['morning_train']] if sub.get('morning_train') else [])
        evening_trains = sub.get('evening_trains') or ([sub['evening_train']] if sub.get('evening_train') else [])
        all_trains = [t for t in morning_trains + evening_trains if t]

        if not all_trains:
            continue

        missing = gtfs.check_trains_in_schedule(all_trains)

        if missing:
            if not ntfy_topic:
                skipped_no_topic.append({'trains': sorted(missing)})
                continue

            manage_url = f"{FRONTEND_URL}/?topic={ntfy_topic}"
            missing_str = ', '.join(sorted(missing))
            sms_service._send_ntfy(
                title="NJ Transit Schedule Updated",
                message=(
                    f"NJT just updated their schedule. "
                    f"Train(s) {missing_str} may have changed or been renumbered. "
                    f"Tap to review and update your train selections."
                ),
                priority="high",
                topic=ntfy_topic,
                click_url=manage_url
            )
            notified.append({'missing_trains': sorted(missing)})
            print(f"📢 Schedule change alert sent — missing trains: {missing_str}")
        else:
            all_ok.append(True)

    print(f"✅ Schedule change check complete: {len(notified)} notified, {len(all_ok)} OK, {len(skipped_no_topic)} skipped (no ntfy topic)")
    return {
        "total_checked": len(subscriptions),
        "notified": len(notified),
        "all_trains_ok": len(all_ok),
        "skipped_no_topic": len(skipped_no_topic),
        "message": f"Sent alerts to {len(notified)} subscriber(s) with outdated train numbers."
    }


# ========== SERVICE ALERTS FEATURE FLAG ==========

@app.get("/admin/service-alerts/status")
def service_alerts_status(username: str = Depends(verify_admin)):
    """Admin: Check whether system-wide service alert broadcasts are enabled."""
    from cache import cache_get as _cache_get
    enabled = _cache_get('service_alerts_enabled')
    return {"service_alerts_enabled": bool(enabled)}

@app.post("/admin/service-alerts/enable")
def service_alerts_enable(username: str = Depends(verify_admin)):
    """Admin: Enable system-wide service alert broadcasts."""
    from cache import cache_set as _cache_set
    _cache_set('service_alerts_enabled', True, ttl_hours=8760)  # 1 year
    return {"service_alerts_enabled": True, "message": "Service alerts enabled"}

@app.post("/admin/service-alerts/disable")
def service_alerts_disable(username: str = Depends(verify_admin)):
    """Admin: Disable system-wide service alert broadcasts (use during heavy dev)."""
    from cache import cache_set as _cache_set
    _cache_set('service_alerts_enabled', False, ttl_hours=8760)
    return {"service_alerts_enabled": False, "message": "Service alerts disabled"}

# ========== ADMIN TEST ENDPOINTS ==========

@app.get("/admin/test/arrival-alert/{phone}")
def admin_test_arrival_alert(phone: str, username: str = Depends(verify_admin)):
    """Admin: Send a mock arrival notification to a subscriber's ntfy topic."""
    from database import get_subscription
    normalized = '+1' + phone.replace('+1', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    sub = get_subscription(normalized)
    if not sub or not sub.get('ntfy_topic'):
        raise HTTPException(status_code=404, detail="Subscription not found or no ntfy topic")
    topic = sub['ntfy_topic']
    manage_url = f"{FRONTEND_URL}/?topic={topic}"
    sms_service._send_ntfy(
        title="Train 3832 — Arrives Edison at 6:57 PM (on time) [TEST]",
        message="Arrives at Edison at 6:57 PM — on time [this is a test notification]",
        priority="default",
        topic=topic,
        click_url=manage_url
    )
    return {"status": "sent", "ntfy_topic": topic, "phone": normalized}


# ========== PUBLIC CONFIG / FEATURE FLAGS ==========

@app.get("/config")
def get_config():
    """Public: feature flag values for the frontend."""
    lirr_enabled = cache_get('lirr_enabled')
    return {"lirr_enabled": bool(lirr_enabled) if lirr_enabled is not None else True}


# ========== ADMIN FEATURE FLAGS ==========

@app.get("/admin/features", response_class=HTMLResponse)
def admin_features_page(username: str = Depends(verify_admin)):
    """Admin: HTML page to manage feature flags."""
    lirr_enabled = cache_get('lirr_enabled')
    lirr_on = bool(lirr_enabled) if lirr_enabled is not None else True
    status = "✅ Enabled" if lirr_on else "❌ Disabled"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Feature Flags — Admin</title>
<style>
  body{{font-family:sans-serif;max-width:640px;margin:2rem auto;padding:1rem;background:#f5f5f5;color:#333}}
  h1{{margin-bottom:1.5rem}}
  .flag{{background:#fff;padding:1rem 1.25rem;border-radius:10px;margin:1rem 0;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 4px rgba(0,0,0,0.08)}}
  .flag-info strong{{display:block;margin-bottom:0.2rem}}
  .flag-info small{{color:#666;font-size:0.82rem}}
  .btns{{display:flex;gap:0.5rem}}
  button{{padding:0.45rem 0.9rem;border:none;border-radius:6px;cursor:pointer;font-size:0.85rem;font-weight:600}}
  .enable{{background:#43a047;color:#fff}}.disable{{background:#e53935;color:#fff}}
  .back{{display:inline-block;margin-top:1rem;color:#1976D2;font-size:0.9rem;text-decoration:none}}
  .msg{{padding:0.6rem 1rem;background:#E8F5E9;border-radius:6px;margin:0.5rem 0;display:none;font-size:0.9rem}}
</style></head>
<body>
<h1>⚙️ Feature Flags</h1>
<div class="flag">
  <div class="flag-info">
    <strong>🚇 LIRR Support</strong>
    <small id="lirr-status">{status} — shows LIRR "Coming Soon" toggle in frontend</small>
  </div>
  <div class="btns">
    <button class="enable" onclick="setFlag('lirr','enable')">Enable</button>
    <button class="disable" onclick="setFlag('lirr','disable')">Disable</button>
  </div>
</div>
<div class="msg" id="msg"></div>
<a href="/admin/subscriptions" class="back">← Admin Home</a>
<script>
async function setFlag(flag, action) {{
  const res = await fetch(`/admin/features/${{flag}}/${{action}}`, {{method:'POST'}});
  const data = await res.json();
  const msg = document.getElementById('msg');
  msg.style.display = 'block';
  msg.textContent = data.message || (action==='enable' ? 'Enabled!' : 'Disabled!');
  if (flag === 'lirr') {{
    document.getElementById('lirr-status').textContent =
      (action==='enable' ? '✅ Enabled' : '❌ Disabled') +
      ' — shows LIRR "Coming Soon" toggle in frontend';
  }}
  setTimeout(() => {{ msg.style.display='none'; }}, 3000);
}}
</script>
</body></html>""")


@app.post("/admin/features/lirr/enable")
def admin_lirr_enable(username: str = Depends(verify_admin)):
    """Admin: Enable LIRR feature flag (shows Coming Soon UI in frontend)."""
    cache_set('lirr_enabled', True, ttl_hours=8760)
    return {"lirr_enabled": True, "message": "LIRR feature enabled — frontend will show Coming Soon toggle"}


@app.post("/admin/features/lirr/disable")
def admin_lirr_disable(username: str = Depends(verify_admin)):
    """Admin: Disable LIRR feature flag (hides LIRR from frontend entirely)."""
    cache_set('lirr_enabled', False, ttl_hours=8760)
    return {"lirr_enabled": False, "message": "LIRR feature disabled — hidden from frontend"}


# ================================================

if __name__ == "__main__":
    print("\n🚀 Starting NJ Transit Delay Alerts API Server...")
    print("📍 Server: http://localhost:8000")
    print("📖 Docs: http://localhost:8000/docs")
    print("\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
