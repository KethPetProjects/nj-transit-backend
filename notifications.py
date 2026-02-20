"""
SMS Notification Service
Currently using MOCK mode - prints instead of sending
Replace with real Twilio when A2P campaign is approved
"""
import os
from typing import Optional

class SMSService:
    """SMS Service (MOCK mode by default, REAL when Twilio approved)"""
    
    def __init__(self, account_sid: Optional[str] = None, 
                 auth_token: Optional[str] = None, 
                 from_number: Optional[str] = None):
        # Load from environment variables if not provided
        self.account_sid = account_sid or os.getenv('TWILIO_ACCOUNT_SID')
        self.auth_token = auth_token or os.getenv('TWILIO_AUTH_TOKEN')
        self.from_number = from_number or os.getenv('TWILIO_PHONE_NUMBER', '+19733142062')
        
        self.mode = "REAL" if (self.account_sid and self.auth_token) else "MOCK"
        print(f"📱 SMS Service initialized ({self.mode} MODE)")
        
        if self.mode == "MOCK":
            print("   ℹ️  Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to enable real SMS")
    
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
        
        return True
    
    def send_verification_code(self, to_number: str, code: str) -> bool:
        """Send verification code SMS"""
        message = f"Your NJ Transit Alerts verification code is: {code}"
        return self.send_sms(to_number, message)
    
    def send_delay_alert(self, to_number: str, train_number: str, delay_minutes: int) -> bool:
        """Send delay alert"""
        message = f"⚠️ DELAY: Train {train_number} is delayed {delay_minutes} minutes."
        return self.send_sms(to_number, message)
    
    def send_cancellation_alert(self, to_number: str, train_number: str) -> bool:
        """Send cancellation alert"""
        message = f"🚫 CANCELLED: Train {train_number} has been cancelled. Check alternative trains."
        return self.send_sms(to_number, message)
    
    def send_ontime_alert(self, to_number: str, train_number: str, departure_time: str) -> bool:
        """Send on-time confirmation"""
        message = f"✅ Train {train_number} is departing on time at {departure_time}. Have a great commute!"
        return self.send_sms(to_number, message)

# Example usage:
if __name__ == '__main__':
    sms = SMSService()
    
    print("\n🧪 Testing SMS service:")
    sms.send_verification_code("+19738208812", "123456")
    sms.send_delay_alert("+19738208812", "3804", 15)
    sms.send_cancellation_alert("+19738208812", "3804")
    sms.send_ontime_alert("+19738208812", "3804", "7:05 AM")
