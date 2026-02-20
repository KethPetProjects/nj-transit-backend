"""
FastAPI Web Server for NJ Transit Delay Alerts
Handles subscription, verification, and unsubscribe
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

from database import (
    save_subscription, 
    verify_subscription, 
    get_subscription,
    delete_subscription,
    get_active_subscriptions
)
from notifications import SMSService
from njtransit import NJTransitAPI

app = FastAPI(title="NJ Transit Delay Alerts API")

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

# Request models
class SubscribeRequest(BaseModel):
    phone: str
    morning_train: str
    evening_train: str
    delay_alerts: bool = True
    ontime_alerts: bool = True

class VerifyRequest(BaseModel):
    phone: str
    code: str

class UnsubscribeRequest(BaseModel):
    phone: str

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
    Subscribe to train alerts
    1. Save subscription (pending)
    2. Generate verification code
    3. Send SMS with code
    """
    try:
        # Save subscription and get verification code
        code = save_subscription(
            phone=request.phone,
            morning_train=request.morning_train,
            evening_train=request.evening_train,
            delay_alerts=request.delay_alerts,
            ontime_alerts=request.ontime_alerts
        )
        
        # Send verification code
        sms_service.send_verification_code(request.phone, code)
        
        return {
            "status": "pending",
            "message": "Verification code sent to your phone"
        }
    
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

@app.post("/unsubscribe")
def unsubscribe(request: UnsubscribeRequest):
    """
    Unsubscribe from alerts
    1. Generate verification code
    2. Send SMS
    3. Return pending status (user confirms in next step)
    """
    try:
        # Check if subscription exists
        sub = get_subscription(request.phone)
        
        if not sub:
            raise HTTPException(status_code=404, detail="Subscription not found")
        
        # For simplicity, just delete immediately
        # In production, you'd want verification here too
        deleted = delete_subscription(request.phone)
        
        if deleted:
            sms_service.send_sms(
                request.phone,
                "You've been unsubscribed from NJ Transit Delay Alerts. Reply STOP to confirm."
            )
            return {
                "status": "unsubscribed",
                "message": "You've been unsubscribed from alerts"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to unsubscribe")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/trains")
def get_trains():
    """Get list of available trains"""
    return {
        "morning": nj_transit.get_available_trains('outbound'),
        "evening": nj_transit.get_available_trains('inbound')
    }

@app.get("/trains/{station_code}")
def get_station_trains(station_code: str):
    """Get real trains for a specific station from NJ Transit API"""
    try:
        trains_data = nj_transit.get_station_schedule(station_code)
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

if __name__ == "__main__":
    print("\n🚀 Starting NJ Transit Delay Alerts API Server...")
    print("📍 Server: http://localhost:8000")
    print("📖 Docs: http://localhost:8000/docs")
    print("\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
