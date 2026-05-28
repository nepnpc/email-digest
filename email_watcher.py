#!/usr/bin/env python3
"""
Email Watcher — Real-time important email alerts via Telegram.

Runs every 30 min via GitHub Actions.
Only alerts on real individual humans writing to you — recruiters,
employers, people. Ignores everything automated/bulk/social.
"""
import os
import re
import sys
import json
import imaplib
import email
import requests
from datetime import datetime, timedelta, timezone, date
from email.header import decode_header
from email.utils import parsedate_to_datetime

from google import genai

# ── Config ──────────────────────────────────────────────────────────────────
GMAIL_EMAIL      = os.environ["GMAIL_EMAIL"]
GMAIL_APP_PASS   = os.environ["GMAIL_APP_PASSWORD"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# check last 24 hours
LOOKBACK_MINUTES = 24 * 60

# ── Decode email headers ────────────────────────────────────────────────────
def decode_str(s):
    if not s:
        return ""
    parts = []
    for chunk, charset in decode_header(s):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def get_text_snippet(msg):
    snippet = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    snippet = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            snippet = payload.decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
    return re.sub(r"\s+", " ", snippet).strip()[:600]


# ── Pre-filter: catch obvious automated emails before hitting Gemini ─────────
SKIP_FROM_PATTERNS = re.compile(
    r"noreply|no-reply|donotreply|do-not-reply|mailer-daemon|"
    r"postmaster|bounce|notifications@|alerts@|newsletter|"
    r"marketing@|hello@.*bulk|support@.*auto",
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "facebookmail.com", "notification.linkedin.com", "em.linkedin.com",
    "bounce.linkedin.com", "linkedin.com", "twitter.com", "x.com",
    "instagram.com", "pinterest.com", "quora.com", "medium.com",
    "mailchimp.com", "sendgrid.net", "mandrillapp.com", "amazonses.com",
}


def is_obviously_automated(msg, sender):
    # bulk/newsletter emails always have List-Unsubscribe or List-ID header
    if msg.get("List-Unsubscribe") or msg.get("List-ID") or msg.get("Precedence"):
        return True

    # noreply patterns in From address
    if SKIP_FROM_PATTERNS.search(sender):
        return True

    # known automated sending domains
    from_domain = re.search(r"@([\w.\-]+)", sender)
    if from_domain:
        domain = from_domain.group(1).lower()
        if domain in SKIP_DOMAINS:
            return True
        # subdomains of skip domains
        for skip in SKIP_DOMAINS:
            if domain.endswith("." + skip):
                return True

    return False


# ── Fetch recent emails via IMAP ────────────────────────────────────────────
def fetch_recent_emails():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_EMAIL, GMAIL_APP_PASS)
    mail.select("INBOX", readonly=True)  # readonly = never marks as read

    today = date.today().strftime("%d-%b-%Y")
    _, msg_ids = mail.search(None, f"SINCE {today}")

    ids = msg_ids[0].split()
    if not ids:
        mail.logout()
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    emails = []

    for msg_id in ids:
        try:
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw = data[0][1]
            msg = email.message_from_bytes(raw)

            # parse and check time
            date_hdr = msg.get("Date", "")
            try:
                email_time = parsedate_to_datetime(date_hdr)
                if email_time.tzinfo is None:
                    email_time = email_time.replace(tzinfo=timezone.utc)
                if email_time < cutoff:
                    continue  # too old
            except Exception:
                continue  # can't parse date — skip

            labels = []  # IMAP doesn't expose Gmail labels easily
            sender  = decode_str(msg.get("From", ""))
            subject = decode_str(msg.get("Subject", "(no subject)"))

            # fast pre-filter before Gemini
            if is_obviously_automated(msg, sender):
                print(f"AUTO-SKIP: {subject[:60]} | {sender[:40]}")
                continue

            snippet = get_text_snippet(msg)

            emails.append({
                "id": msg_id.decode(),
                "from": sender,
                "subject": subject,
                "snippet": snippet,
                "time": email_time.strftime("%H:%M"),
            })

        except Exception as e:
            print(f"Warning: failed {msg_id}: {e}", file=sys.stderr)

    mail.logout()
    return emails


# ── Gemini: is this a real important individual email? ──────────────────────
CLASSIFY_PROMPT = """You screen emails for a person. Decide if each email is from a REAL INDIVIDUAL human
(recruiter, employer, colleague, client, friend, family) writing personally — vs automated/bulk/marketing.

ALERT if:
- Real recruiter or HR person personally reaching out about a job
- Real employer writing about an opportunity, interview, or offer
- Individual human from a company writing directly (not automated)
- Personal email from a real person about anything important
- Freelance/contract opportunity from a real human
- Any real person writing that requires attention or response

IGNORE if:
- LinkedIn notifications, activity alerts, "X viewed your profile"
- Any automated email, newsletter, bulk marketing
- System notifications, security alerts from platforms (unless it's your own bank/account)
- Any email from a no-reply or automated sender that slipped through

Respond ONLY with valid JSON array, no markdown:
[{"index": 0, "alert": true, "reason": "recruiter from Google asking about SWE role"}, ...]

Emails:
"""


def classify_emails(emails):
    if not emails:
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)

    lines = [
        f'[{i}]\nFROM: {e["from"]}\nSUBJECT: {e["subject"]}\nPREVIEW: {e["snippet"]}'
        for i, e in enumerate(emails)
    ]

    try:
        resp = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=CLASSIFY_PROMPT + "\n\n".join(lines),
        )
        text = resp.text.strip()

        if "```" in text:
            for chunk in text.split("```"):
                chunk = chunk.strip().lstrip("json").strip()
                try:
                    return json.loads(chunk)
                except Exception:
                    continue

        return json.loads(text)

    except Exception as e:
        print(f"Warning: Gemini error: {e}", file=sys.stderr)
        # on error, alert everything that passed pre-filter (never miss)
        return [{"index": i, "alert": True, "reason": "AI error — kept safe"}
                for i in range(len(emails))]


# ── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if not resp.ok:
        print(f"Telegram error: {resp.text}", file=sys.stderr)


def format_batch_telegram_message(alerted_emails):
    today = datetime.now().strftime("%B %d, %Y")
    lines = [f"📬 <b>Morning Email Report — {today}</b>\n"]
    lines.append(f"{len(alerted_emails)} important email(s) need your attention:\n")

    for i, (em, reason) in enumerate(alerted_emails, 1):
        sender  = em["from"][:70]
        subject = em["subject"][:100]
        preview = em["snippet"][:200]
        lines.append(
            f"──────────────\n"
            f"<b>{i}. {subject}</b>\n"
            f"From: {sender}\n"
            f"<i>{preview}</i>\n"
            f"🤖 {reason}"
        )

    lines.append(f'\n──────────────\n<a href="https://mail.google.com">Open Gmail →</a>')

    full = "\n\n".join(lines)

    # Telegram limit: 4096 chars — trim preview if needed
    if len(full) > 4000:
        full = full[:3990] + "\n...(truncated)"

    return full


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Email Watcher | Checking last {LOOKBACK_MINUTES} min ===")

    # if manually triggered, send test ping so Telegram can be verified
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual:
        send_telegram("✅ <b>Email Watcher is working!</b>\n\nTelegram connection confirmed. You'll get pinged here when a real important email arrives.")
        print("Test ping sent to Telegram")

    emails = fetch_recent_emails()
    print(f"Passed pre-filter: {len(emails)} emails")

    if not emails:
        print("Nothing to check — done")
        return

    results = classify_emails(emails)
    idx_map = {r["index"]: r for r in results}

    alerted_emails = []
    for i, em in enumerate(emails):
        result = idx_map.get(i, {})
        should_alert = result.get("alert", True)  # default True = never miss
        reason = result.get("reason", "")

        if should_alert:
            alerted_emails.append((em, reason))
            print(f"ALERT: {em['subject'][:60]}")
        else:
            print(f"SKIP:  {em['subject'][:60]}")

    if alerted_emails:
        msg = format_batch_telegram_message(alerted_emails)
        send_telegram(msg)
        print(f"Sent 1 Telegram message with {len(alerted_emails)} email(s)")
    else:
        print("No important emails today — Telegram silent")

    print(f"Done: {len(alerted_emails)} important out of {len(emails)} checked")


if __name__ == "__main__":
    main()
