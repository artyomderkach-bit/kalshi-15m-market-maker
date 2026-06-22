"""SMTP email helper shared by the watchdog (daily summary) and the weekly
model-calibration report.

Configured entirely by env (.env on the server):
    SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
    SMTP_USER, SMTP_PASS, EMAIL_FROM (default SMTP_USER), EMAIL_TO.
For Gmail/Google Workspace use a 16-char App Password, not the login password.
Never raises — returns a short status string for logging.
"""
import os
import smtplib
from email.message import EmailMessage


def send_email(subject: str, body: str) -> str:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    to_addr = os.getenv("EMAIL_TO", "")
    from_addr = os.getenv("EMAIL_FROM", user)
    if not (user and password and to_addr):
        return "email skipped (set SMTP_USER, SMTP_PASS, EMAIL_TO in .env)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return f"email sent to {to_addr}"
    except Exception as e:
        return f"email FAILED: {e}"
