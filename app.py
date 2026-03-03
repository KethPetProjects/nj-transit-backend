"""
FastAPI Web Server for NJ Transit Delay Alerts
Handles subscription, verification, and unsubscribe
"""
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date, timedelta
import uvicorn
import secrets
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
    morning_train: str
    evening_train: str
    delay_alerts: bool = True
    ontime_alerts: bool = True
    station: str = ''  # 2-char NJT station code for home station (e.g. 'PJ')

class VerifyRequest(BaseModel):
    phone: str
    code: str


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

        result = save_subscription(
            phone=phone,
            morning_train=request.morning_train,
            evening_train=request.evening_train,
            delay_alerts=request.delay_alerts,
            ontime_alerts=request.ontime_alerts,
            station=request.station
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
def get_station_trains(station_code: str, schedule: str = 'weekday'):
    """Get trains for a specific station. schedule=weekday|saturday|sunday"""
    try:
        query_date = _representative_date(schedule)
        trains_data = nj_transit.get_station_schedule(station_code, query_date=query_date)
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
    return {
        "morning_train": sub["morning_train"],
        "evening_train": sub["evening_train"],
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

# ================================================

if __name__ == "__main__":
    print("\n🚀 Starting NJ Transit Delay Alerts API Server...")
    print("📍 Server: http://localhost:8000")
    print("📖 Docs: http://localhost:8000/docs")
    print("\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
