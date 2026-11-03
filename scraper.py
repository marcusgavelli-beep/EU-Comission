
import os
import re
import json
import sqlite3
import hashlib
import urllib.request
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

CASE_URL = "https://tron.trade.ec.europa.eu/investigations/case-history?caseId=1531"
DB_FILE = "seen_items.db"


def get_page_text():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            try:
                page.goto(CASE_URL, wait_until="networkidle", timeout=60000)
            except PlaywrightTimeoutError:
                print("Page took a long time to finish loading; checking visible content.")

            page.wait_for_timeout(10000)
            text = page.locator("body").inner_text()
            return "\n".join(
                re.sub(r"\s+", " ", line).strip()
                for line in text.splitlines()
                if line.strip()
            )
        finally:
            browser.close()


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = json.dumps({
        "chat_id": chat_id,
        "text": message[:4000]
    }).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram did not accept the message.")


def main():
    print("====================================")
    print("TRON CASE 1531 MONITOR")
    print("====================================")

    print("Loading the case history in a browser...")
    text = get_page_text()

    print("Visible text characters:", len(text))

    if len(text) < 200:
        raise RuntimeError(
            "The case history did not load. No monitoring data was changed."
        )

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS case1531_state (
                id INTEGER PRIMARY KEY,
                page_hash TEXT NOT NULL,
                page_text TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )
        """)

        previous = db.execute(
            "SELECT page_hash, page_text FROM case1531_state WHERE id = 1"
        ).fetchone()

        if previous is None:
            db.execute(
                "INSERT INTO case1531_state VALUES (1, ?, ?, ?)",
                (digest, text, datetime.now(timezone.utc).isoformat())
            )
            print("FIRST CHECK: saved the current page as the baseline.")
            print("No alert sent. Future changes will trigger alerts.")
            return

        old_hash, old_text = previous

        if digest == old_hash:
            print("NO CHANGE: the case history is unchanged.")
            return

        old_lines = set(old_text.splitlines())
        new_lines = text.splitlines()
        added = [line for line in new_lines if line not in old_lines]

        message = (
            "EU COMMISSION - TRON CASE 1531\n\n"
            "The case-history page has changed.\n\n"
        )

        if added:
            message += "New or changed text:\n" + "\n".join(added[:40])
        else:
            message += "The page content changed. Please check the case history."

        message += "\n\n" + CASE_URL

        print("CHANGE DETECTED. Sending Telegram alert...")
        send_telegram(message)

        db.execute(
            "UPDATE case1531_state SET page_hash=?, page_text=?, checked_at=? WHERE id=1",
            (digest, text, datetime.now(timezone.utc).isoformat())
        )

        print("Telegram alert sent and monitoring state updated.")


if __name__ == "__main__":
    main()
