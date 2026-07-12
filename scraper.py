#!/usr/bin/env python3
"""
Daily scraper for beer prices on cenoteka.rs

What it does:
  - Phase 1: walks every listing page (/pivo/, /pivo/p/2/, ...) to discover
    every product's slug/name/url/image/discount.
  - Phase 2: visits each product's OWN page (/p/{slug}/) to read its full
    price comparison table -- this is where ALL shops show up, including
    the ones the listing page hides behind "+N prodavnice". If a product's
    detail page fails to fetch/parse for some reason, we fall back to
    whatever prices we already had from the listing page for that product,
    so a single bad request doesn't lose the whole day's data for it.
  - Saves everything into beer_prices.db, one row per (product, shop, date).

Usage:
    python3 scrape_beer.py
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
    "User-Agent": ("Mozilla/5.0 (compatible; BeerPriceTracker/1.0; personal-use-scraper)"),
    "Accept-Language": "sr,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

REQUEST_DELAY_SECONDS = 3.0
MAX_PAGES_SAFETY_CAP = 60
MAX_RETRIES_ON_RATE_LIMIT = 6
BASE_BACKOFF_SECONDS = 20

# Data quality guardrails
MIN_EXPECTED_PRODUCTS = 100  # if the listing scrape finds fewer than this, something's
# probably broken (site changed, we got blocked, etc.) --
# abort rather than silently writing a half-empty day.
MIN_REASONABLE_PRICE = 10.0  # RSD. A parsed price outside this range is almost
MAX_REASONABLE_PRICE = 3000.0  # certainly a parsing bug, not a real beer price.

SHOP_SLUG_FROM_IMG_RE = re.compile(r"shop-([a-z0-9]+)-")
MULTIPACK_VOLUME_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+(?:[.,]\d+)?)\s*(ml|l)\b")
SINGLE_VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|l)\b")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def get_page_url(page_num: int) -> str:
    if page_num == 1:
        return BASE_URL
    return PAGE_URL_TEMPLATE.format(page=page_num)


def fetch_url(url: str) -> str | None:
    """
    Fetches a URL, automatically waiting out and retrying on HTTP 429 (rate
    limited). If the server sends a Retry-After header we honor it exactly;
    otherwise we back off for BASE_BACKOFF_SECONDS * attempt (20s, 40s, 60s...).
    Gives up after MAX_RETRIES_ON_RATE_LIMIT attempts and returns None, which
    the caller treats as "skip this one for today".
    """
    for attempt in range(1, MAX_RETRIES_ON_RATE_LIMIT + 1):
        try:
            resp = SESSION.get(url, timeout=20)
        except requests.RequestException as e:
            print(f"  ! Network error fetching {url}: {e}")
            return None

        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = BASE_BACKOFF_SECONDS * attempt
            else:
                wait_seconds = BASE_BACKOFF_SECONDS * attempt

            print(
                f"  ! 429 rate limited on {url} "
                f"(attempt {attempt}/{MAX_RETRIES_ON_RATE_LIMIT}), "
                f"waiting {wait_seconds:.0f}s ..."
            )
            time.sleep(wait_seconds)
            continue

        print(f"  ! Unexpected status {resp.status_code} for {url}")
        return None

    print(f"  ! Giving up on {url} after {MAX_RETRIES_ON_RATE_LIMIT} rate-limit retries")
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_price(text: str):
    """'110,99' -> 110.99 ; ignores stray characters like 'RSD'."""
    cleaned = text.replace("RSD", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def shop_slug_from_img(img_tag) -> str:
    """
    Shop image filenames always look like '...shop-{slug}-{hash}.png',
    regardless of which page we're on. We use that instead of the alt text
    because alt text is inconsistent between pages (e.g. listing page uses
    the raw slug 'tempo', the product detail page uses the display name
    'Mega Maxi' for the exact same shop).
    """
    for attr in ("src", "srcset"):
        val = img_tag.get(attr, "")
        m = SHOP_SLUG_FROM_IMG_RE.search(val)
        if m:
            return m.group(1)
    # Fallback: alt text, lowercased and spaces removed.
    return (img_tag.get("alt") or "").strip().lower().replace(" ", "-")


def parse_volume_liters(name: str) -> float | None:
    """
    Extracts total volume in liters from a product name, e.g.:
      'LAV 0,5l' -> 0.5
      'TUBORG pivo staklo 0,0% 330ml' -> 0.33
      'Pivo KABINET ... 4x0,33l' -> 1.32 (multipack)
    Returns None if no volume pattern is found.
    """
    m = MULTIPACK_VOLUME_RE.search(name)
    if m:
        count = int(m.group(1))
        unit_val = float(m.group(2).replace(",", "."))
        liters = unit_val / 1000 if m.group(3) == "ml" else unit_val
        return round(count * liters, 4)

    m = SINGLE_VOLUME_RE.search(name)
    if m:
        unit_val = float(m.group(1).replace(",", "."))
        liters = unit_val / 1000 if m.group(2) == "ml" else unit_val
        return round(liters, 4)

    return None


def parse_price_rows(rows) -> list[dict]:
    """Given an iterable of '.row' elements each containing a shop img + a
    .product_price div, extract (shop, price, cheapest, promo) dicts.
    Prices outside a sane RSD range are dropped with a warning -- almost
    certainly a parsing bug rather than a real price, and we'd rather skip
    one data point than pollute the dataset with something like 0.01 RSD."""
    results = []
    for row in rows:
        shop_img = row.find("img")
        price_div = row.find("div", class_="product_price")
        if shop_img is None or price_div is None:
            continue
        price = parse_price(price_div.get_text(strip=True))
        if price is None:
            continue
        if not (MIN_REASONABLE_PRICE <= price <= MAX_REASONABLE_PRICE):
            print(f"    ! skipping implausible price {price} RSD (outside sane range)")
            continue
        classes = price_div.get("class", [])
        results.append(
            {
                "shop": shop_slug_from_img(shop_img),
                "price": price,
                "cheapest": "cheapest" in classes,
                "promo": "promo_price" in classes,
            }
        )
    return results


def parse_listing_products(html: str) -> list[dict]:
    """Phase 1: parse a /pivo/ listing page into a list of product dicts.
    We only need slug/name/url/image/discount here -- prices always come
    from each product's own detail page in phase 2, since that's the only
    place the FULL shop list shows up."""
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

        products.append(
            {
                "slug": slug,
                "name": name,
                "url": url,
                "image": image,
                "discount": discount,
            }
        )

    return products


def parse_detail_prices(html: str) -> list[dict]:
    """
    Phase 2: parse a product's own page (/p/{slug}/) for its FULL price
    comparison table. `prices_offline_col` wraps only this product's own
    rows -- it deliberately does NOT reuse a selector like `.product_price`
    on the whole page, because the "similar products" carousel further down
    the same page reuses that exact same price-badge markup for OTHER
    products, and we don't want to accidentally attribute their prices to
    this one.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("div.prices_offline_col")
    if container is None:
        return []
    rows = container.find_all("div", class_="row", recursive=False)
    return parse_price_rows(rows)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            image_url TEXT
        );

        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            shop TEXT NOT NULL,
            price REAL NOT NULL,
            promo INTEGER NOT NULL DEFAULT 0,
            cheapest INTEGER NOT NULL DEFAULT 0,
            discount TEXT,
            scrape_date TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            UNIQUE(product_id, shop, scrape_date)
        );

        CREATE INDEX IF NOT EXISTS idx_prices_product ON prices(product_id);
        CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(scrape_date);

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scrape_date TEXT NOT NULL,
            products_discovered INTEGER,
            products_skipped INTEGER,
            price_rows_recorded INTEGER,
            status TEXT NOT NULL DEFAULT 'running'  -- running | ok | partial | failed
        );
        """
    )
    conn.commit()

    # Migration: older databases won't have this column yet.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(products)")]
    if "volume_liters" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN volume_liters REAL")
        conn.commit()


def start_run(conn, scrape_date: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, scrape_date, status) VALUES (?, ?, 'running')",
        (started_at, scrape_date),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn, run_id, finished_at, products_discovered, products_skipped, price_rows_recorded, status
):
    conn.execute(
        """
        UPDATE scrape_runs
        SET finished_at = ?, products_discovered = ?, products_skipped = ?,
            price_rows_recorded = ?, status = ?
        WHERE id = ?
        """,
        (finished_at, products_discovered, products_skipped, price_rows_recorded, status, run_id),
    )
    conn.commit()


def upsert_product(conn, slug, name, url, image_url):
    volume_liters = parse_volume_liters(name)
    conn.execute(
        """
        INSERT INTO products (slug, name, url, image_url, volume_liters)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name=excluded.name, url=excluded.url, image_url=excluded.image_url,
            volume_liters=excluded.volume_liters
        """,
        (slug, name, url, image_url, volume_liters),
    )
    return conn.execute("SELECT id FROM products WHERE slug=?", (slug,)).fetchone()[0]


def upsert_price(
    conn, product_id, shop, price, promo, cheapest, discount, scrape_date, scraped_at
):
    conn.execute(
        """
        INSERT INTO prices (product_id, shop, price, promo, cheapest, discount, scrape_date, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id, shop, scrape_date)
        DO UPDATE SET price=excluded.price, promo=excluded.promo,
                      cheapest=excluded.cheapest, scraped_at=excluded.scraped_at
        """,
        (product_id, shop, price, promo, cheapest, discount, scrape_date, scraped_at),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    today = date.today().isoformat()
    started_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    run_id = start_run(conn, today, started_at)

    # Phase 1: discover every product from the listing pages.
    all_products = {}  # slug -> product dict
    page = 1
    while page <= MAX_PAGES_SAFETY_CAP:
        url = get_page_url(page)
        print(f"[listing] page {page} ({url}) ...")
        html = fetch_url(url)
        if html is None:
            print("  no more pages (or fetch failed). Stopping listing phase.")
            break

        products = parse_listing_products(html)
        if not products:
            print("  no products found on this page. Stopping listing phase.")
            break

        for p in products:
            all_products[p["slug"]] = p
        print(f"  -> {len(products)} products on this page ({len(all_products)} total so far).")

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    # Health check: if we found way fewer products than expected, something's
    # probably broken (site layout changed, we got blocked, etc). Abort
    # rather than silently overwrite good historical data with a bad day.
    if len(all_products) < MIN_EXPECTED_PRODUCTS:
        print(
            f"\n! Only found {len(all_products)} products, expected at least "
            f"{MIN_EXPECTED_PRODUCTS}. Something looks wrong (site layout change? "
            f"blocked?). Aborting WITHOUT writing any price data for {today}.\n"
        )
        finish_run(
            conn,
            run_id,
            datetime.now(timezone.utc).isoformat(),
            products_discovered=len(all_products),
            products_skipped=None,
            price_rows_recorded=0,
            status="failed",
        )
        conn.close()
        return 1

    print(f"\nDiscovered {len(all_products)} products. Fetching full price details for each...\n")

    # Phase 2: visit each product's own page for the complete price list.
    # This is now the ONLY source of prices -- the listing page isn't
    # scraped for prices at all, since it never has the full shop list.
    total_price_rows = 0
    skipped_count = 0

    for i, (slug, p) in enumerate(all_products.items(), start=1):
        detail_url = p["url"]
        print(f"[detail {i}/{len(all_products)}] {slug} ...")
        html = fetch_url(detail_url)

        prices = parse_detail_prices(html) if html is not None else []

        if not prices:
            skipped_count += 1
            print(f"  ! no prices found (fetch or parse failed) -- skipping for today")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"  -> {len(prices)} shops")

        now_iso = datetime.now(timezone.utc).isoformat()
        product_id = upsert_product(conn, slug, p["name"], p["url"], p["image"])
        for price_info in prices:
            upsert_price(
                conn,
                product_id,
                price_info["shop"],
                price_info["price"],
                price_info["promo"],
                price_info["cheapest"],
                p["discount"],
                today,
                now_iso,
            )
            total_price_rows += 1

        conn.commit()
        time.sleep(REQUEST_DELAY_SECONDS)

    skip_ratio = skipped_count / len(all_products) if all_products else 1.0
    status = "ok" if skip_ratio < 0.1 else "partial"

    finish_run(
        conn,
        run_id,
        datetime.now(timezone.utc).isoformat(),
        products_discovered=len(all_products),
        products_skipped=skipped_count,
        price_rows_recorded=total_price_rows,
        status=status,
    )
    conn.close()
    print(
        f"\nDone ({status}). {len(all_products)} products discovered, {total_price_rows} price "
        f"points recorded for {today}. ({skipped_count} products skipped today because their "
        f"detail page didn't fetch or parse -- they'll be retried on the next run.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
