import smtplib
import sys
import os
from email.mime.text import MIMEText
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Ensure Windows console supports UTF-8 for printing emojis
try:
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

def send_test_email(to_email, subject="Test Message", body="Hello! This is a test email sent locally."):
    smtp_host = os.getenv("SMTP_HOST", "127.0.0.1")
    # Default to 2525 if not defined in env, but use SMTP_PORT from env if present
    smtp_port = int(os.getenv("SMTP_PORT", 2525))
    
    # If host is 0.0.0.0, connect to 127.0.0.1 locally
    if smtp_host == "0.0.0.0":
        smtp_host = "127.0.0.1"
        
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = "sender@example.com"
    msg["To"] = to_email
    
    print(f"Connecting to SMTP server at {smtp_host}:{smtp_port}...")
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.sendmail("sender@example.com", [to_email], msg.as_string())
        print(f"✅ Success: Email sent to {to_email}!")
    except Exception as e:
        print(f"❌ Error sending email: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python send_test_mail.py <inbox_email_address> [subject] [body]")
        print("Example: python send_test_mail.py something@damxd.shop")
    else:
        email_addr = sys.argv[1]
        subj = sys.argv[2] if len(sys.argv) > 2 else "Test Message"
        message_body = sys.argv[3] if len(sys.argv) > 3 else "Hello! This is a test email sent locally."
        send_test_email(email_addr, subj, message_body)
