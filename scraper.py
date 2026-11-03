
import os
import re
import json
import time
import sqlite3
import hashlib
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CASE_URL = (
    "https://tron.trade.ec.europa.eu/"
    "investigations/case-history?caseId=1531"
)

DB_PATH = "seen_items.db"


# --------------------------------------------------
# HTML READER
# --------------------------------------------------

class CasePageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []
        self.current_link = None
        self.ignore_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in ("script", "style", "noscript"):
            self.ignore_depth += 1
            return

        if self.ignore_depth:
            return

        if tag == "a" and attrs.get("href"):
            self.current_link = {
                "url": attrs["href"],
                "title": ""
            }

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            if self.ignore_depth:
                self.ignore_depth -= 1
            return

        if tag == "a" and self.current_link:
            title = clean(self.current_link["title"])

            if title:
                self.links.append({
                    "url": self.current_link["url"],
                    "title": title
                })

            self.current_link = None

    def handle_data(self, data):
        if self.ignore_depth:
            return

        value = clean(data)

        if not value:
            return

        self.text_parts.append(value)

        if self.current_link is not None:
            self.current_link["title"] += " " + value


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


# --------------------------------------------------
# DOWNLOAD CASE PAGE
# --------------------------------------------------

def download_case_page():

    request = urllib.request.Request(
        CASE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 EUCase1531Monitor/1.0",
            "Accept-Language": "en-GB,en;q=0.9"
        }
    )

    with urllib.request.urlopen(request, timeout=45) as response:
        html = response.read().decode(
            "utf-8",
            errors="replace"
        )

    parser = CasePageParser()
    parser.feed(html)

    return html, parser


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def initialise_database():

    db = sqlite3.connect(DB_PATH)

    db.execute("""
        CREATE TABLE IF NOT EXISTS monitor_state (
            monitor_name TEXT PRIMARY KEY,
            content_hash TEXT,
            last_checked TEXT
        )
    """)

    db.commit()
    return db


def get_previous_hash(db):

    result = db.execute(
        """
        SELECT content_hash
        FROM monitor_state
        WHERE monitor_name = ?
        """,
        ("case_1531",)
    ).fetchone()

    return result[0] if result else None


def save_hash(db, digest):

    db.execute(
        """
        INSERT INTO monitor_state
        (monitor_name, content_hash, last_checked)
        VALUES (?, ?, ?)
        ON CONFLICT(monitor_name)
        DO UPDATE SET
            content_hash = excluded.content_hash,
            last_checked = excluded.last_checked
        """,
        (
            "case_1531",
            digest,
            datetime.now(timezone.utc).isoformat()
        )
    )

    db.commit()


# --------------------------------------------------
# TELEGRAM
# --------------------------------------------------

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing."
        )

    api_url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }).encode("utf-8")

    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram returned an unsuccessful response."
        )


def send_test():

    send_telegram(
        "✅ CASE 1531 MONITOR CONNECTED\n\n"
        "Your TRON Case 1531 monitoring system "
        "has successfully connected to Telegram."
    )


# --------------------------------------------------
# MONITOR CASE 1531
# --------------------------------------------------

def main():

    print("====================================")
    print("TRON CASE 1531 MONITOR")
    print("====================================")

    db = initialise_database()

    try:

        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            send_test()
            print("Telegram test message delivered.")

        print("Checking:", CASE_URL)

        html, parser = download_case_page()

        page_text = " ".join(parser.text_parts)

        print("Downloaded HTML characters:", len(html))
        print("Extracted text characters:", len(page_text))
        print("Extracted links:", len(parser.links))

        # A very small response may mean TRON did not return
        # the actual case-history content.
        if len(page_text) < 100:
            raise RuntimeError(
                "Very little readable content was returned. "
                "TRON may be loading the case history dynamically. "
                "The previous monitoring state was not changed."
            )

        # Build a stable representation of the visible page.
        # Ignore whitespace changes.
        link_data = [
            (clean(item["title"]), item["url"])
            for item in parser.links
        ]

        snapshot = json.dumps({
            "text": page_text,
            "links": link_data
        }, sort_keys=True, ensure_ascii=False)

        current_hash = hashlib.sha256(
            snapshot.encode("utf-8")
        ).hexdigest()

        previous_hash = get_previous_hash(db)

        if previous_hash is None:

            print("FIRST CHECK: saving initial baseline.")

            save_hash(db, current_hash)

            print(
                "Baseline saved. No alert sent because "
                "there is no previous version to compare."
            )

        elif current_hash == previous_hash:

            print("NO CHANGE: Case 1531 is unchanged.")

        else:

            print("CHANGE DETECTED IN CASE 1531.")

            message = (
                "🇪🇺 TRON CASE 1531 UPDATE\n\n"
                "A change has been detected on the official "
                "case-history page.\n\n"
                "Please open the page to inspect the new "
                "publication or change:\n\n"
                + CASE_URL
            )

            # Save the new version only after Telegram confirms
            # that the alert was successfully delivered.
            send_telegram(message)
            save_hash(db, current_hash)

            print("Telegram update alert delivered.")

        print("Check completed successfully.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
