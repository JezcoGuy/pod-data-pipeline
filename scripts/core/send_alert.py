"""
send_alert.py
Usage: python3 send_alert.py "Subject" "Body message"
Used by cron to send failure alerts.
"""
import sys
import smtplib
import os
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv('/opt/your_brand_id/.env')

subject = sys.argv[1] if len(sys.argv) > 1 else 'Your Brand cron alert'
body    = sys.argv[2] if len(sys.argv) > 2 else 'A cron job failed.'

msg = EmailMessage()
msg['Subject'] = f'[Your Brand] {subject}'
msg['From']    = os.getenv('SMTP_FROM')
msg['To']      = os.getenv('SMTP_TO')
msg.set_content(body)

with smtplib.SMTP_SSL(os.getenv('SMTP_HOST'), int(os.getenv('SMTP_PORT', 465))) as s:
    s.login(os.getenv('SMTP_USER'), os.getenv('SMTP_PASS'))
    s.send_message(msg)

print(f'Alert sent: {subject}')