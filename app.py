import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from flask import Flask, flash, render_template, redirect, url_for

load_dotenv()

print(os.path.abspath(".env"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "steakos-dev-secret")

GMAIL_IMAP_HOST = "imap.gmail.com"
MAILBOX_COUNT = 5
SEARCH_WINDOW_MINUTES = 30


def load_search_criteria() -> dict:
    return {
        "sender": os.getenv("SEARCH_SENDER", "").strip(),
        "subject": os.getenv("SEARCH_SUBJECT", "").strip(),
    }


def load_mailboxes() -> list[dict]:
    mailboxes = []
    for i in range(1, MAILBOX_COUNT + 1):
        address = os.getenv(f"EMAIL_{i}_ADDRESS", "").strip()
        password = os.getenv(f"EMAIL_{i}_PASSWORD", "").strip()
        mailboxes.append(
            {
                "id": str(i),
                "address": address,
                "password": password,
                "configured": bool(address and password),
            }
        )
    print(mailboxes)    
    return mailboxes


def get_mailbox_by_id(mailboxes: list[dict], mailbox_id: str) -> dict | None:
    for mailbox in mailboxes:
        if mailbox["id"] == mailbox_id:
            return mailbox
    return None


def decode_mime_value(value: str | None) -> str:
    if not value:
        return ""

    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def extract_html_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/html" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""

    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def extract_links(html: str) -> list[str]:
    if not html:
        return []

    href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    seen: set[str] = set()
    links: list[str] = []
    for match in href_pattern.findall(html):
        link = match.strip()
        if not link or link.startswith("#") or link.lower().startswith("mailto:"):
            continue
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def parse_received_at(msg: Message, internal_date: bytes | None = None) -> datetime | None:
    if internal_date:
        try:
            date_text = internal_date.decode("utf-8", errors="replace")
            parsed = datetime.strptime(date_text, "%d-%b-%Y %H:%M:%S %z")
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

    date_header = msg.get("Date")
    if not date_header:
        return None

    try:
        parsed = parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def matches_search_criteria(
    sender: str, subject: str, criteria: dict
) -> bool:
    search_sender = criteria["sender"]
    search_subject = criteria["subject"]

    if not search_sender and not search_subject:
        return True

    sender_match = (
        search_sender.lower() in sender.lower() if search_sender else False
    )
    subject_match = (
        search_subject.lower() in subject.lower() if search_subject else False
    )
    return sender_match or subject_match


def imap_since_date(cutoff: datetime) -> str:
    return cutoff.strftime("%d-%b-%Y")


def fetch_recent_emails(
    address: str, password: str, criteria: dict, mailbox_label: str | None = None
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=SEARCH_WINDOW_MINUTES)
    mail = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
    try:
        mail.login(address, password)
        mail.select("INBOX")

        status, data = mail.search(None, f"(SINCE {imap_since_date(cutoff)})")
        if status != "OK":
            raise RuntimeError("無法搜尋信件")

        message_ids = data[0].split()
        if not message_ids:
            return []

        results = []
        for message_id in message_ids:
            status, msg_data = mail.fetch(message_id, "(RFC822 INTERNALDATE)")
            if status != "OK" or not msg_data:
                continue

            raw_email = None
            internal_date = None
            for item in msg_data:
                if not isinstance(item, tuple):
                    continue
                header = item[0]
                if isinstance(header, bytes):
                    internal_match = re.search(
                        rb'INTERNALDATE "([^"]+)"', header
                    )
                    if internal_match:
                        internal_date = internal_match.group(1)
                if isinstance(item[1], bytes):
                    raw_email = item[1]

            if not raw_email:
                continue

            msg = email.message_from_bytes(raw_email)
            sender = decode_mime_value(msg.get("From"))
            subject = decode_mime_value(msg.get("Subject"))
            received_at = parse_received_at(msg, internal_date)

            if received_at is None or received_at < cutoff:
                continue
            if not matches_search_criteria(sender, subject, criteria):
                continue

            body_html = extract_html_body(msg)
            results.append(
                {
                    "mailbox": mailbox_label or address,
                    "subject": subject or "（無主旨）",
                    "from": sender or "（未知寄件者）",
                    "received_at": received_at,
                    "received_at_display": received_at.astimezone().strftime(
                        "%Y-%m-%d %H:%M:%S %Z"
                    ),
                    "body_html": body_html,
                    "links": extract_links(body_html),
                }
            )

        results.sort(key=lambda item: item["received_at"], reverse=True)
        return results
    finally:
        try:
            mail.logout()
        except imaplib.IMAP4.error:
            pass


def fetch_all_mailboxes(
    mailboxes: list[dict], criteria: dict
) -> tuple[list[dict], list[str]]:
    all_results: list[dict] = []
    errors: list[str] = []

    for mailbox in mailboxes:
        if not mailbox["configured"]:
            continue
        try:
            emails = fetch_recent_emails(
                mailbox["address"],
                mailbox["password"],
                criteria,
                mailbox_label=mailbox["address"],
            )
            all_results.extend(emails)
        except imaplib.IMAP4.error:
            errors.append(mailbox["address"])
        except Exception:
            errors.append(mailbox["address"])

    all_results.sort(key=lambda item: item["received_at"], reverse=True)
    return all_results, errors


def search_mailbox(mailbox_id: str):
    mailboxes = load_mailboxes()
    criteria = load_search_criteria()

    if mailbox_id == "all":
        configured = [m for m in mailboxes if m["configured"]]
        if not configured:
            flash("尚未設定任何信箱，請確認 .env 中的帳號與密碼", "error")
            return redirect(url_for("index"))

        if not criteria["sender"] and not criteria["subject"]:
            flash("請在 .env 設定 SEARCH_SENDER 或 SEARCH_SUBJECT", "error")
            return redirect(url_for("index"))

        emails, errors = fetch_all_mailboxes(mailboxes, criteria)
        if errors:
            flash(f'以下信箱登入失敗：{", ".join(errors)}', "error")
        if errors and not emails:
            return redirect(url_for("index"))

        return render_template(
            "index.html",
            mailboxes=mailboxes,
            criteria=criteria,
            emails=emails,
            selected_mailbox="all",
            searched=True,
        )

    mailbox = get_mailbox_by_id(mailboxes, mailbox_id)
    if mailbox is None:
        flash("找不到指定的信箱", "error")
        return redirect(url_for("index"))

    if not mailbox["configured"]:
        flash("此信箱尚未設定，請確認 .env 中的帳號與密碼", "error")
        return redirect(url_for("index"))

    if not criteria["sender"] and not criteria["subject"]:
        flash("請在 .env 設定 SEARCH_SENDER 或 SEARCH_SUBJECT", "error")
        return redirect(url_for("index"))

    try:
        emails = fetch_recent_emails(
            mailbox["address"], mailbox["password"], criteria
        )
    except imaplib.IMAP4.error:
        flash("信箱登入失敗，請確認 .env 中的帳號與應用程式密碼是否正確", "error")
        return redirect(url_for("index"))
    except Exception:
        flash("讀取信件時發生錯誤，請稍後再試", "error")
        return redirect(url_for("index"))

    return render_template(
        "index.html",
        mailboxes=mailboxes,
        criteria=criteria,
        emails=emails,
        selected_mailbox=mailbox_id,
        searched=True,
    )


@app.route("/")
def index():
    mailboxes = load_mailboxes()
    criteria = load_search_criteria()
    return render_template(
        "index.html",
        mailboxes=mailboxes,
        criteria=criteria,
        emails=None,
        selected_mailbox=None,
        searched=False,
    )


@app.route("/mailbox/<mailbox_id>")
def mailbox_search(mailbox_id: str):
    return search_mailbox(mailbox_id)


if __name__ == "__main__":
    app.run(debug=True)
