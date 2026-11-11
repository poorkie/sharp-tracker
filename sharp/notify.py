"""Alert delivery: ntfy (free push), Twilio SMS, email, or console."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger(__name__)


def send(channels: list[str], title: str, body: str, url: str | None = None) -> None:
    for ch in channels:
        try:
            {"ntfy": _ntfy, "twilio": _twilio, "email": _email,
             "console": _console}[ch](title, body, url)
        except KeyError:
            log.error("unknown alert channel %r", ch)
        except Exception as e:  # keep going if one channel fails
            log.error("alert via %s failed: %s", ch, e)


def _console(title, body, url):
    print(f"\n=== {title} ===\n{body}\n{url or ''}\n")


def _ntfy(title, body, url):
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "moneybag"}
    if url:
        headers["Click"] = url
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                  headers=headers, timeout=20).raise_for_status()


def _twilio(title, body, url):
    sid, token = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]
    text = f"{title}\n{body}" + (f"\n{url}" if url else "")
    for to in os.environ["ALERT_TO_PHONE"].split(","):
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": os.environ["TWILIO_FROM"], "To": to.strip(), "Body": text[:1500]},
            auth=(sid, token), timeout=20).raise_for_status()


def _email(title, body, url):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_EMAIL_TO"]
    msg.set_content(body + (f"\n\n{url}" if url else ""))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), timeout=20) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
