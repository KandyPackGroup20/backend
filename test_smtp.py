import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
smtp_port = int(os.getenv("SMTP_PORT", 587))
smtp_user = os.getenv("SMTP_USER", "sachinigunasekara2004@gmail.com").strip()
smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()

target = sys.argv[1] if len(sys.argv) > 1 else "scriptspectra@gmail.com"

print(f"Testing SMTP transmission:")
print(f"  Server: {smtp_host}:{smtp_port}")
print(f"  Sender: {smtp_user}")
print(f"  Target: {target}")

if not smtp_pass:
    print("\n[ERROR] SMTP_PASSWORD is not set in backend/.env!")
    print("Please generate a 16-character Google App Password for sachinigunasekara2004@gmail.com")
    print("and set: SMTP_PASSWORD=your_app_password in backend/.env")
    sys.exit(1)

msg = MIMEMultipart("alternative")
msg["Subject"] = "[KandyPack] Live Email Delivery Test"
msg["From"] = f"KandyPack Logistics <{smtp_user}>"
msg["To"] = target
msg.attach(MIMEText(f"Hello! This is a test email dispatched directly from {smtp_user} via Gmail SMTP to {target}.", "plain"))

try:
    with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [target], msg.as_string())
    print(f"\n[SUCCESS] Live email successfully dispatched to {target}!")
except Exception as e:
    print(f"\n[SMTP ERROR] {e}")
    sys.exit(1)
