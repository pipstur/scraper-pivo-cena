#!/usr/bin/env python3
"""
Daily scraper for beer prices on cenoteka.rs

What it does:
  - Walks every page of https://cenoteka.rs/pivo/ (and /pivo/p/2/, /pivo/p/3/, ...)
  - For each product, records the price at each shop that's shown on the listing page
  - Saves everything into a local SQLite database (beer_prices.db), one row per
    (product, shop, date) so you build up a price history over time
  - Safe to re-run the same day: it won't create duplicate rows for the same date

Usage:
    python3 scrape_beer.py

Requires: requests, beautifulsoup4  (pip install requests beautifulsoup4)
"""

import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cenoteka.rs/pivo/"
PAGE_URL_TEMPLATE = "https://cenoteka.rs/pivo/p/{page}/"
DB_PATH = "beer_prices.db"

HEADERS = {
    # Look like an ordinary browser; be a polite, identifiable bot.
    "User-Agent": ("Mozilla/5.0 (compatible; BeerPriceTracker/1.0; personal-use-scraper)"),
    "Accept-Language": "sr,en;q=0.8",
}

REQUEST_DELAY_SECONDS = 1.5  # be polite between page requests
MAX_PAGES_SAFETY_CAP = 60  # stop even if something goes wrong with detection

PRODUCT_LINK_RE = re.compile(r"^/p/[^/]+/$")


def get_page_url(page_num: int) -> str:
    if page_num == 1:
        return BASE_URL
    return PAGE_URL_TEMPLATE.format(page=page_num)


def fetch_page(page_num: int) -> str | None:
    url = get_page_url(page_num)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        print(f"  ! Network error fetching {url}: {e}")
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"  ! Unexpected status {resp.status_code} for {url}")
        return None
    return resp.text


def parse_products(html):
    soup = BeautifulSoup(html, "html.parser")

    products = []

    for card in soup.select("div.product_wrap"):
        info = card.select_one("div.product_info a")
        if info is None:
            continue

        name = info.get_text(strip=True)
        url = info["href"]
        slug = url.rstrip("/").split("/")[-1]

        image = None
        img = card.select_one("a.product_image img")
        if img:
            image = img.get("src")

        discount = None
        discount_tag = card.select_one(".product_savings span")
        if discount_tag:
            discount = discount_tag.get_text(strip=True)

        prices = []

        for row in card.select("div.product_info_wrap > div.row"):
            shop_img = row.select_one("img[alt]")
            price_div = row.select_one(".product_price")

            if not shop_img or not price_div:
                continue

            price = parse_price(price_div.get_text(strip=True))
            if price is None:
                continue

            classes = price_div.get("class", [])

            prices.append(
                {
                    "shop": shop_img["alt"].lower(),
                    "price": price,
                    "cheapest": "cheapest" in classes,
                    "promo": "promo_price" in classes,
                }
            )

        products.append(
            {
                "slug": slug,
                "name": name,
                "url": url,
                "image": image,
                "discount": discount,
                "prices": prices,
            }
        )

    return products


def parse_price(text: str):
    """'110,99' -> 110.99 ; ignores stray characters like 'RSD'."""
    cleaned = text.replace("RSD", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")  # handle thousands sep too
    try:
        return float(cleaned)
    except ValueError:
        return None


def init_db(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            shop TEXT NOT NULL,
            price REAL NOT NULL,
            scrape_date TEXT NOT NULL,     -- YYYY-MM-DD
            scraped_at TEXT NOT NULL,      -- full ISO timestamp of this run
            UNIQUE(product_id, shop, scrape_date)
        );

        CREATE INDEX IF NOT EXISTS idx_prices_product ON prices(product_id);
        CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(scrape_date);
        """
    )
    conn.commit()


def upsert_product(conn: sqlite3.Connection, slug: str, name: str, url: str) -> int:
    conn.execute(
        """
        INSERT INTO products (slug, name, url) VALUES (?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET name=excluded.name, url=excluded.url
        """,
        (slug, name, url),
    )
    cur = conn.execute("SELECT id FROM products WHERE slug = ?", (slug,))
    return cur.fetchone()[0]


def upsert_price(conn, product_id, shop, price, scrape_date, scraped_at):
    conn.execute(
        """
        INSERT INTO prices (product_id, shop, price, scrape_date, scraped_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(product_id, shop, scrape_date)
        DO UPDATE SET price=excluded.price, scraped_at=excluded.scraped_at
        """,
        (product_id, shop, price, scrape_date, scraped_at),
    )


def main():
    today = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total_products = 0
    total_price_rows = 0
    page = 1

    while page <= MAX_PAGES_SAFETY_CAP:
        print(f"Fetching page {page} ({get_page_url(page)}) ...")
        html = fetch_page(page)
        if html is None:
            print("  No more pages (or fetch failed). Stopping.")
            break

        products = parse_products(html)
        if not products:
            print("  No products found on this page. Stopping.")
            break

        for p in products:
            product_id = upsert_product(conn, p["slug"], p["name"], p["url"])
            total_products += 1
            for shop, price in p["prices"]:
                upsert_price(conn, product_id, shop, price, today, now_iso)
                total_price_rows += 1

        conn.commit()
        print(f"  -> {len(products)} products parsed.")

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(
        f"\nDone. {total_products} product listings processed, "
        f"{total_price_rows} price points recorded for {today}."
    )


if __name__ == "__main__":
    sys.exit(main())
