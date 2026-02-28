"""
SMS Notification Service
Currently using MOCK mode - prints instead of sending
Replace with real Twilio when A2P campaign is approved

ntfy.sh push notifications: set NTFY_TOPIC env var to receive real
push notifications on your phone while Twilio is pending approval.
"""
import os
import requests
from typing import Optional

NTFY_BASE = "https://ntfy.sh"

class SMSService:
    """SMS Service (MOCK mode by default, REAL when Twilio approved)"""

    def __init__(self, account_sid: Optional[str] = None,
                 auth_token: Optional[str] = None,
                 from_number: Optional[str] = None):
        # Load from environment variables if not provided
        self.account_sid = account_sid or os.getenv('TWILIO_ACCOUNT_SID')
        self.auth_token = auth_token or os.getenv('TWILIO_AUTH_TOKEN')
        self.from_number = from_number or os.getenv('TWILIO_PHONE_NUMBER', '+19733142062')
        self.ntfy_topic = os.getenv('NTFY_TOPIC')

        self.mode = "REAL" if (self.account_sid and self.auth_token) else "MOCK"
        print(f"📱 SMS Service initialized ({self.mode} MODE)")

        if self.mode == "MOCK":
            print("   ℹ️  Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to enable real SMS")
        if self.ntfy_topic:
            print(f"   🔔 ntfy.sh push notifications enabled (topic: {self.ntfy_topic})")

    def _send_ntfy(self, title: str, message: str, priority: str = "default") -> None:
        """Send a push notification via ntfy.sh (best-effort, never raises)."""
        if not self.ntfy_topic:
            return
        try:
            requests.post(
                f"{NTFY_BASE}/{self.ntfy_topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": "train",
                },
                timeout=5
            )
            print(f"   🔔 ntfy.sh notification sent")
        except Exception as e:
            print(f"   ⚠️ ntfy.sh failed (non-critical): {e}")

    def send_sms(self, to_number: str, message: str) -> bool:
        """
        Send SMS (MOCK - just prints)

        Real implementation:
        from twilio.rest import Client
        client = Client(self.account_sid, self.auth_token)
        client.messages.create(body=message, from_=self.from_number, to=to_number)
        """
        print(f"\n📱 SMS SENT (MOCK)")
        print(f"   To: {to_number}")
        print(f"   From: {self.from_number}")
        print(f"   Message: {message}")
        print(f"   ✓ Delivered (simulated)")

        # Also push via ntfy.sh if configured
        self._send_ntfy("NJ Transit Alert", message)

        return True
    
    def send_verification_code(self, to_number: str, code: str) -> bool:
        """Send verification code SMS"""
        message = f"Your NJ Transit Alerts verification code is: {code}"
        return self.send_sms(to_number, message)
    
    def send_delay_alert(self, to_number: str, train_number: str, delay_minutes: int) -> bool:
        """Send delay alert"""
        message = f"⚠️ DELAY: Train {train_number} is delayed {delay_minutes} minutes."
        print(f"\n📱 SMS SENT (MOCK)\n   To: {to_number}\n   Message: {message}\n   ✓ Delivered (simulated)")
        self._send_ntfy(f"Train {train_number} Delayed", message, priority="high")
        return True

    def send_cancellation_alert(self, to_number: str, train_number: str) -> bool:
        """Send cancellation alert"""
        message = f"🚫 CANCELLED: Train {train_number} has been cancelled. Check alternative trains."
        print(f"\n📱 SMS SENT (MOCK)\n   To: {to_number}\n   Message: {message}\n   ✓ Delivered (simulated)")
        self._send_ntfy(f"Train {train_number} Cancelled", message, priority="urgent")
        return True

    def send_ontime_alert(self, to_number: str, train_number: str, departure_time: str) -> bool:
        """Send on-time confirmation"""
        message = f"✅ Train {train_number} is departing on time at {departure_time}. Have a great commute!"
        print(f"\n📱 SMS SENT (MOCK)\n   To: {to_number}\n   Message: {message}\n   ✓ Delivered (simulated)")
        self._send_ntfy(f"Train {train_number} On Time", message, priority="default")
        return True

# Example usage:
if __name__ == '__main__':
    sms = SMSService()
    
    print("\n🧪 Testing SMS service:")
    sms.send_verification_code("+19738208812", "123456")
    sms.send_delay_alert("+19738208812", "3804", 15)
    sms.send_cancellation_alert("+19738208812", "3804")
    sms.send_ontime_alert("+19738208812", "3804", "7:05 AM")
