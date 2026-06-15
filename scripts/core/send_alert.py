"""
send_alert.py
Usage: python3 send_alert.py "Subject" "Body message"
Used by cron to send failure alerts.

Supports both STARTTLS (port 587, default) and implicit SSL (port 465).
Many VPS providers block 465 — 587 is the safer default.
"""
import sys
import smtplib
import os
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()  # relies on cron's `cd` to repo root

subject = sys.argv[1] if len(sys.argv) > 1 else 'Your Brand cron alert'
body    = sys.argv[2] if len(sys.argv) > 2 else 'A cron job failed.'

host = os.getenv('SMTP_HOST')
port = int(os.getenv('SMTP_PORT', '587'))
user = os.getenv('SMTP_USER') or os.getenv('SMTP_FROM')
pw   = os.getenv('SMTP_PASS')

msg = EmailMessage()
msg['Subject'] = f'[Your Brand] {subject}'
msg['From']    = os.getenv('SMTP_FROM') or user
msg['To']      = os.getenv('SMTP_TO')
msg.set_content(body)

if port == 465:
    # Implicit SSL — used when 587 is blocked or the provider requires it
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, pw)
        s.send_message(msg)
else:
    # STARTTLS — recommended default (port 587)
    with smtplib.SMTP(host, port) as s:
        s.ehlo()
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)

print(f'Alert sent: {subject}')
