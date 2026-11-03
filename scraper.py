
import os
import re
import json
import time
import sqlite3
import hashlib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from datetime import datetime, timezone


# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DB_PATH = "seen_items.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 EUTradeDefenceMonitor/2.0",
    "Accept-Language": "en-GB,en;q=0.9",
}

PRODUCT_TERMS = [
    "silicon metal",
    "silicon",
    "ferrosilicon",
    "ferro-silicon",
    "ferro silicon",
    "ferrosilicon",
    "silicon-based alloying",
    "silicon based alloying",
]

TRADE_TERMS = [
    "anti-dumping",
    "anti dumping",
    "anti-subsidy",
    "anti subsidy",
    "countervailing",
    "trade defence",
    "trade defense",
    "safeguard",
    "investigation",
    "expiry review",
    "interim review",
    "new exporter review",
    "anti-circumvention",
    "anti circumvention",
    "provisional measures",
    "definitive measures",
    "import duties",
    "customs duties",
    "registration of imports",
]

TRON_BASE = "https://tron.trade.ec.europa.eu"

TRON_PAGES = [
    TRON_BASE + "/investigations/home",
    TRON_BASE + "/investigations/search",
]

# Known case-history records to check.
# Additional relevant cases are discovered from TRON's case listings.
TRON_CASE_IDS = [
    "1531",
    "201",
    "412",
    "2900",
]

EURLEX_URLS = [
    "https://eur-lex.europa.eu/search.html?" +
    urllib.parse.urlencode({
        "text": '"silicon metal" OR ferrosilicon OR "ferro-silicon"',
        "scope": "EURLEX",
        "lang": "en",
    }),
]

COMMISSION_URLS = [
    "https://policy.trade.ec.europa.eu/news_en",
]


# ==================================================
# HTML READER
# ==================================================

class HTMLReader(HTMLParser):

    def __init__(self):
        super().__init__()
        self.links = []
        self.text = []
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
                "title": "",
            }

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            if self.ignore_depth:
                self.ignore_depth -= 1
            return

        if tag == "a" and self.current_link:
            title = clean(self.current_link["title"])

            if title:
                self.current_link["title"] = title
                self.links.append(self.current_link)

            self.current_link = None

    def handle_data(self, data):
        if self.ignore_depth:
            return

        value = clean(data)

        if not value:
            return

        self.text.append(value)

        if self.current_link is not None:
            self.current_link["title"] += " " + value


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


# ==================================================
# DOWNLOAD
# ==================================================

def download(url):
    request = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read()


def download_text(url):
    return download(url).decode("utf-8", errors="replace")


def read_html(url):
    parser = HTMLReader()
    parser.feed(download_text(url))
    return parser


def absolute_url(base, link):
    return urllib.parse.urljoin(base, link)


# ==================================================
# LANGUAGE AND DOMAIN FILTERS
# ==================================================

def is_english(url):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()

    excluded = [
        "/pl/",
        "/pt/",
        "/fr/",
        "/de/",
        "/es/",
        "/it/",
        "/nl/",
        "/sv/",
        "/da/",
        "/fi/",
        "/el/",
        "/cs/",
        "/sk/",
        "/sl/",
        "/hu/",
        "/ro/",
        "/bg/",
        "/hr/",
        "/et/",
        "/lv/",
        "/lt/",
    ]

    if any(item in path for item in excluded):
        return False

    # EUR-Lex English documents.
    if "eur-lex.europa.eu" in parsed.netloc:
        return "/legal-content/en/" in path

    return True


def allowed_domain(url):
    host = urllib.parse.urlparse(url).netloc.lower()

    return (
        host.endswith("europa.eu")
        or host.endswith("ec.europa.eu")
    )


# ==================================================
# RELEVANCE FILTER
# ==================================================

def is_relevant(text):
    text = text.lower()

    has_product = any(
        term in text for term in PRODUCT_TERMS
    )

    has_trade_action = any(
        term in text for term in TRADE_TERMS
    )

    return has_product and has_trade_action


# ==================================================
# DATABASE
# ==================================================

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


def make_id(url):
    return hashlib.sha256(
        url.split("#")[0].encode("utf-8")
    ).hexdigest()


def already_seen(db, item_id):
    return db.execute(
        "SELECT 1 FROM seen WHERE item_id = ?",
        (item_id,),
    ).fetchone() is not None


def mark_seen(db, item_id, title, url):
    db.execute(
        """
        INSERT OR IGNORE INTO seen
        (item_id, title, url, first_seen)
        VALUES (?, ?, ?, ?)
        """,
        (
            item_id,
            title,
            url,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


# ==================================================
# TELEGRAM
# ==================================================

def telegram_send(message):

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing."
        )

    url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    if not result.get("ok"):
        raise RuntimeError(str(result))


def send_test():

    telegram_send(
        "TEST SUCCESSFUL\n\n"
        "Your EU Silicon Metal and Ferrosilicon "
        "Trade Defence Monitor is connected."
    )


def send_alert(title, url, source):

    message = (
        "🇪🇺 EU TRADE DEFENCE ALERT\n\n"
        "PRODUCT: Silicon metal / Ferrosilicon\n\n"
        f"SOURCE: {source}\n\n"
        f"{title}\n\n"
        f"OFFICIAL LINK:\n{url}"
    )

    telegram_send(message)


# ==================================================
# ALERT PROCESSING
# ==================================================

def process_item(db, title, url, source):

    title = clean(title)

    if not title or len(title) < 5:
        return 0

    if not url.startswith("https://"):
        return 0

    if not allowed_domain(url):
        return 0

    if not is_english(url):
        return 0

    item_id = make_id(url)

    if already_seen(db, item_id):
        return 0

    # Check title first.
    relevant = is_relevant(title)

    # If title is inconclusive, inspect the linked page.
    if not relevant:
        try:
            page = read_html(url)
            full_text = " ".join(page.text)
            relevant = is_relevant(title + " " + full_text)
        except Exception as error:
            print("Article inspection failed:", error)
            return 0

    if not relevant:
        return 0

    print("RELEVANT:", title)

    try:
        send_alert(title, url, source)
        mark_seen(db, item_id, title, url)
        print("Telegram alert delivered.")
        time.sleep(1)
        return 1

    except Exception as error:
        print("Telegram delivery failed:", error)
        return 0


# ==================================================
# TRON RSS DISCOVERY
# ==================================================

def discover_rss_links(page_url, parser):

    feeds = []

    for link in parser.links:
        url = absolute_url(page_url, link["url"])

        if "rss" in url.lower() or "feed" in url.lower():
            if url.startswith("https://"):
                feeds.append(url)

    return list(dict.fromkeys(feeds))


def process_rss(db, feed_url, source):

    print("Checking RSS:", feed_url)

    try:
        raw = download(feed_url)
        root = ET.fromstring(raw)
    except Exception as error:
        print("RSS could not be read:", error)
        return 0

    total = 0

    # Supports RSS and Atom feeds.
    for item in root.iter():
        tag = item.tag.split("}")[-1].lower()

        if tag not in ("item", "entry"):
            continue

        title = ""
        link = ""

        for child in item:
            name = child.tag.split("}")[-1].lower()

            if name == "title":
                title = clean("".join(child.itertext()))

            elif name == "link":
                link = (
                    child.attrib.get("href")
                    or clean("".join(child.itertext()))
                )

        if title and link:
            link = absolute_url(feed_url, link)
            total += process_item(
                db, title, link, source
            )

    return total


# ==================================================
# TRON CASE DISCOVERY
# ==================================================

def get_case_ids_from_page(page_url, parser):

    case_ids = set()

    for link in parser.links:
        url = absolute_url(page_url, link["url"])

        match = re.search(
            r"(?:caseId|caseid)=(\d+)",
            url,
            re.IGNORECASE,
        )

        if match:
            case_ids.add(match.group(1))

    # Also inspect page text for case numbers where available.
    page_text = " ".join(parser.text)

    if is_relevant(page_text):
        print("Relevant product wording found in TRON listing.")

    return case_ids


def process_tron_case(db, case_id):

    history_url = (
        TRON_BASE
        + "/investigations/case-history?caseId="
        + urllib.parse.quote(case_id)
    )

    print("Checking TRON case:", case_id)

    try:
        parser = read_html(history_url)
    except Exception as error:
        print("Could not read case history:", error)
        return 0

    page_text = " ".join(parser.text)
    links = parser.links

    # A case is relevant if its history identifies a target product.
    product_match = any(
        term in page_text.lower()
        for term in PRODUCT_TERMS
    )

    if not product_match:
        print("No silicon/ferrosilicon match in this case.")
        return 0

    total = 0

    # Each publication in the case history may link to EUR-Lex.
    for link in links:

        title = clean(link["title"])
        url = absolute_url(history_url, link["url"])

        if not title:
            continue

        if not (
            "eur-lex.europa.eu" in url
            or "europa.eu" in url
        ):
            continue

        total += process_item(
            db,
            title,
            url,
            "TRON case " + case_id,
        )

    # Also monitor any RSS feeds linked from the case page.
    feeds = discover_rss_links(history_url, parser)

    for feed in feeds:
        total += process_rss(
            db,
            feed,
            "TRON case " + case_id,
        )

    return total


def process_tron(db):

    print("\n========== TRON ==========")

    total = 0
    discovered_cases = set(TRON_CASE_IDS)
    discovered_feeds = set()

    for page_url in TRON_PAGES:

        print("Reading TRON listing:", page_url)

        try:
            parser = read_html(page_url)
        except Exception as error:
            print("TRON listing failed:", error)
            continue

        discovered_cases.update(
            get_case_ids_from_page(page_url, parser)
        )

        discovered_feeds.update(
            discover_rss_links(page_url, parser)
        )

        # Scan visible listing links for product-related cases.
        for link in parser.links:

            title = clean(link["title"])
            url = absolute_url(page_url, link["url"])

            if (
                "case-view" in url
                and is_relevant(title)
            ):
                match = re.search(
                    r"caseId=(\d+)",
                    url,
                    re.IGNORECASE,
                )

                if match:
                    discovered_cases.add(match.group(1))

    print(
        "TRON case IDs to inspect:",
        sorted(discovered_cases),
    )

    for case_id in sorted(discovered_cases):

        total += process_tron_case(
            db,
            case_id,
        )

    for feed in discovered_feeds:

        total += process_rss(
            db,
            feed,
            "TRON new investigations",
        )

    return total


# ==================================================
# EUR-LEX AND COMMISSION NEWS
# ==================================================

def process_listing(db, url, source):

    print("\nChecking:", source)

    try:
        parser = read_html(url)
    except Exception as error:
        print("Source could not be read:", error)
        return 0

    total = 0

    for link in parser.links:

        title = clean(link["title"])
        target = absolute_url(url, link["url"])

        if not title:
            continue

        if not allowed_domain(target):
            continue

        if not is_english(target):
            continue

        # EUR-Lex and Commission listing pages.
        total += process_item(
            db,
            title,
            target,
            source,
        )

    print("New alerts from source:", total)
    return total


# ==================================================
# MAIN
# ==================================================

def main():

    db = initialise_database()

    try:

        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            send_test()
            print("Telegram test message delivered.")

        total = 0

        total += process_tron(db)

        for url in EURLEX_URLS:
            total += process_listing(
                db,
                url,
                "EUR-Lex",
            )

        for url in COMMISSION_URLS:
            total += process_listing(
                db,
                url,
                "European Commission",
            )

        print("\n================================")
        print("SCRAPER FINISHED")
        print("New alerts sent:", total)
        print("================================")

    finally:
        db.close()


if __name__ == "__main__":
    main()
