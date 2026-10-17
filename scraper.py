import os
import re
import sqlite3
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = "seen_items.db"

PRODUCTS = {
    "silicon": [
        "silicon", "silicon metal", "ferrosilicon", "ferro-silicon"
    ],
    "aluminium": [
        "aluminium", "aluminum", "aluminium alloy"
    ],
    "fertilisers": [
        "fertiliser", "fertiliser", "fertilizer", "fertilizers",
        "fertilisers", "urea", "ammonium nitrate"
    ],
    "steel": [
        "steel", "stainless steel", "steel products"
    ],
}

TRADE_DEFENCE_TERMS = [
    "anti-dumping", "anti dumping", "anti-subsidy",
    "anti subsidy", "countervailing", "safeguard",
    "trade defence", "trade defense", "expiry review",
    "expiry", "review", "investigation", "provisional duty",
    "definitive duty", "registration of imports",
    "circumvention", "undertaking", "import duty",
    "imposing a definitive", "imposing a provisional",
    "repealing", "repeal", "extension of the measures",
]

SEARCHES = [
    (
        "EUR-Lex",
        "https://eur-lex.europa.eu/search.html?scope=EURLEX"
        "&type=quick&lang=en&text="
        + urllib.parse.quote(
            '"anti-dumping" OR "countervailing" OR safeguard '
            'silicon aluminium steel fertilisers'
        ),
    ),
    (
        "European Commission",
        "https://policy.trade.ec.europa.eu/news_en",
    ),
]

def fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "EU-Trade-Defence-Alerts/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")

def clean_html(text):
    text = re.sub(r"<(script|style).*?</\1>", " ", text,
                  flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()

def find_links(html, base_url):
    pattern = r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    results = []
    for href, title in re.findall(pattern, html, re.I | re.S):
        title = clean_html(title)
        href = urllib.parse.urljoin(base_url, unescape(href))
        if title and href.startswith("http"):
            results.append((title, href))
    return results

def matches(text):
    text = text.lower()
    product_matches = [
        product for product, terms in PRODUCTS.items()
        if any(term in text for term in terms)
    ]
    defence_matches = [
        term for term in TRADE_DEFENCE_TERMS if term in text
    ]
    return product_matches, defence_matches

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials are not configured yet.")
        return

    import json
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": True,
    }).encode()
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")

def main():
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
    new_count = 0

    for source, url in SEARCHES:
        try:
            page = fetch(url)
            links = find_links(page, url)
            print(f"{source}: found {len(links)} links")
        except Exception as exc:
            print(f"{source}: fetch failed: {exc}")
            continue

        for title, link in links:
            try:
                if source == "EUR-Lex" and "eur-lex.europa.eu" not in link:
                    continue
                if source == "European Commission" and "policy.trade.ec.europa.eu" not in link:
                    continue

                combined = title
                products, defence = matches(combined)
                if not products or not defence:
                    continue

                item_id = hashlib.sha256(link.encode()).hexdigest()
                exists = db.execute(
                    "SELECT 1 FROM seen WHERE item_id = ?",
                    (item_id,)
                ).fetchone()
                if exists:
                    continue

                db.execute(
                    "INSERT INTO seen VALUES (?, ?, ?, ?)",
                    (
                        item_id, title, link,
                        datetime.now(timezone.utc).isoformat()
                    )
                )
                db.commit()
                new_count += 1
                message = (
                    "🇪🇺 EU TRADE DEFENCE ALERT\n\n"
                    f"Source: {source}\n"
                    f"Products: {', '.join(products)}\n"
                    f"Topic: {', '.join(defence[:5])}\n\n"
                    f"{title}\n\n{link}"
                )
                print(message)
                send_telegram(message)
            except Exception as exc:
                print(f"Could not process {link}: {exc}")

    print(f"Finished. New matching alerts: {new_count}")
    db.close()

if __name__ == "__main__":
    main()
