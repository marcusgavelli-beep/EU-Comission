import os
import re
import json
import time
import sqlite3
import hashlib
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = "seen_items.db"

PRODUCTS = {
    "Silicon": ["silicon metal", "silicon", "ferrosilicon", "ferro-silicon"],
    "Aluminium": ["aluminium", "aluminum", "aluminium alloy", "aluminum alloy"],
    "Fertilisers": ["fertiliser", "fertilizer", "fertilisers", "fertilizers", "urea", "ammonium nitrate"],
    "Steel": ["stainless steel", "steel products", "steel", "hot-rolled", "cold-rolled"]
}

DEFENCE_TERMS = [
    "anti-dumping", "anti dumping", "anti-subsidy", "anti subsidy",
    "countervailing", "safeguard", "trade defence", "trade defense",
    "expiry review", "interim review", "new exporter review",
    "circumvention", "provisional duty", "definitive duty",
    "provisional measures", "definitive measures",
    "investigation initiated", "investigation opened",
    "registration of imports", "import duties"
]

SOURCES = [
    ("European Commission",
     "https://policy.trade.ec.europa.eu/news_en"),
    ("Trade Defence Cases",
     "https://tron.trade.ec.europa.eu/investigations/home"),
    ("EUR-Lex",
     "https://eur-lex.europa.eu/search.html?scope=EURLEX&type=quick&lang=en&text="
     + urllib.parse.quote('"anti-dumping" OR "countervailing" OR safeguard'))
]

HEADERS = {
    "User-Agent": "EU-Trade-Defence-Monitor/2.0"
}


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None
        self.text = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in ("script", "style", "noscript"):
            self.ignored += 1

        if tag == "a" and not self.ignored:
            if attrs.get("href"):
                self.current = [attrs["href"], []]

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.ignored = max(0, self.ignored - 1)

        if tag == "a" and self.current:
            title = " ".join(self.current[1])
            title = re.sub(r"\s+", " ", title).strip()

            if title:
                self.links.append((title, self.current[0]))

            self.current = None

    def handle_data(self, data):
        if self.ignored:
            return

        data = data.strip()

        if data:
            self.text.append(data)

            if self.current:
                self.current[1].append(data)


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8", errors="replace")


def extract(html, base_url):
    parser = Parser()
    parser.feed(html)

    links = []

    for title, href in parser.links:
        url = urllib.parse.urljoin(base_url, href)

        if urllib.parse.urlparse(url).scheme in ("http", "https"):
            links.append((title, url))

    return links, " ".join(parser.text)


def matches(text):
    text = text.lower()

    products = [
        name for name, terms in PRODUCTS.items()
        if any(term in text for term in terms)
    ]

    topics = [
        term for term in DEFENCE_TERMS
        if term in text
    ]

    return products, topics


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram credentials are missing.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": True
    }).encode()

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def initialise_database():
    db = sqlite3.connect(DB_PATH)

    db.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            item_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            first_seen TEXT
        )
    """)

    db.commit()
    return db


def already_seen(db, item_id):
    return db.execute(
        "SELECT 1 FROM seen WHERE item_id = ?",
        (item_id,)
    ).fetchone() is not None


def mark_seen(db, item_id, title, url):
    db.execute("""
        INSERT OR IGNORE INTO seen
        VALUES (?, ?, ?, ?)
    """, (
        item_id,
        title,
        url,
        datetime.now(timezone.utc).isoformat()
    ))

    db.commit()


def allowed_link(source, url):
    host = urllib.parse.urlparse(url).netloc.lower()

    if source == "European Commission":
        return host == "policy.trade.ec.europa.eu"

    if source == "Trade Defence Cases":
        return host == "tron.trade.ec.europa.eu"

    if source == "EUR-Lex":
        return host.endswith("eur-lex.europa.eu")

    return False


def process_item(db, source, title, url):
    item_id = hashlib.sha256(url.encode()).hexdigest()

    if already_seen(db, item_id):
        return False

    try:
        article_html = fetch(url)
        parser = Parser()
        parser.feed(article_html)
        article_text = " ".join(parser.text)
    except Exception as exc:
        print(f"Article fetch failed: {exc}")
        article_text = ""

    products, topics = matches(title + " " + article_text)

    if not products or not topics:
        return False

    message = (
        "🇪🇺 EU TRADE DEFENCE ALERT\n\n"
        f"Source: {source}\n"
        f"Products: {', '.join(products)}\n"
        f"Topics: {', '.join(topics[:5])}\n\n"
        f"{title}\n\n{url}"
    )

    send_telegram(message)
    mark_seen(db, item_id, title, url)

    print(f"ALERT SENT: {title}")
    return True


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing.")

    db = initialise_database()

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send_telegram(
            "🇪🇺 EU TRADE DEFENCE MONITOR\n\n"
            "Telegram connection test successful!"
        )
        print("Telegram test delivered.")

    total_links = 0
    total_alerts = 0

    for source, source_url in SOURCES:
        print(f"\nChecking {source}...")

        try:
            html = fetch(source_url)
            links, page_text = extract(html, source_url)
            print(f"{source}: found {len(links)} links")
            total_links += len(links)

        except Exception as exc:
            print(f"SOURCE ERROR: {source}: {exc}")
            continue

        checked = 0

        for title, url in links:
            if checked >= 100 or total_alerts >= 10:
                break

            if not allowed_link(source, url):
                continue

            if any(word in url.lower() for word in [
                "login", "contact", "search.html?scope"
            ]):
                continue

            checked += 1

            try:
                if process_item(db, source, title, url):
                    total_alerts += 1

                time.sleep(0.5)

            except Exception as exc:
                print(f"ITEM ERROR: {title}: {exc}")

    print("\n" + "=" * 45)
    print(f"Total links found: {total_links}")
    print(f"New alerts sent: {total_alerts}")
    print("Monitoring completed.")
    print("=" * 45)

    db.close()


if __name__ == "__main__":
    main()
