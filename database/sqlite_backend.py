"""
Shared data-access helpers for the beer price dashboard.
Matches the schema produced by scrape_beer.py:

products(id, slug, name, url, image_url)
prices(id, product_id, shop, price, promo, cheapest, discount, scrape_date, scraped_at)
"""

import sqlite3
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd

from geopy.geocoders import Nominatim
from functools import lru_cache

geolocator = Nominatim(user_agent="beer-price-dashboard")

# pandas 3.0 made "StringDtype" the default for text columns. Some pyarrow
# builds choke on that dtype when Streamlit serializes a dataframe for
# display (crashes ranging from a clean exception to a segfault). Forcing
# the legacy plain-object string dtype avoids that code path entirely.
pd.set_option("future.infer_string", False)

DB_PATH = Path(__file__).parent / "beer_prices.db"

SHOP_DISPLAY_NAMES = {
    "lidl": "Lidl",
    "dis": "DIS",
    "univerexport": "Univerexport",
    "maxi": "Maxi",
    "idea": "IDEA",
    "roda": "Roda",
    "tempo": "Mega Maxi",
    "probar": "Probar",
    "aman": "Aman",
    "persu": "Persu",
    "gomex": "Gomex",
    "nis": "NIS Petrol",
}
SHOP_SEARCH_NAMES = {
    "lidl": "Lidl supermarket",
    "dis": "DIS supermarket",
    "univerexport": "Univerexport supermarket",
    "maxi": "Maxi supermarket",
    "tempo": "Mega Maxi supermarket",
    "idea": "IDEA supermarket",
    "roda": "Roda supermarket",
    "probar": "ProBar supermarket",
    "aman": "Aman supermarket",
    "persu": "PerSu supermarket",
    "gomex": "Gomex supermarket",
    "nis": "NIS Petrol",
}

CHAIN_COORDS = {
    "lidl": (44.7866, 20.4489),
    "dis": (43.3209, 21.8958),
    "univerexport": (45.2671, 19.8335),
    "maxi": (44.8125, 20.4612),
    "idea": (44.0165, 21.0059),
    "roda": (44.7866, 20.3489),
    "tempo": (45.2517, 19.8369),
    "probar": (44.9530, 20.4650),
    "aman": (43.1367, 21.9189),
    "persu": (44.7000, 20.9000),
    "gomex": (44.0000, 20.9000),
    "nis": (44.8000, 20.5500),
}
DEFAULT_COORDS = (44.0165, 20.9114)


def shop_display_name(slug: str) -> str:
    return SHOP_DISPLAY_NAMES.get(slug, slug.title())


def maps_search_url(shop_slug: str) -> str:
    query = f"{shop_display_name(shop_slug)} Serbia"
    return f"https://www.google.com/maps/search/{quote_plus(query)}"


@lru_cache(maxsize=None)
def get_shop_coords(shop_slug):
    query = SHOP_SEARCH_NAMES.get(shop_slug, shop_display_name(shop_slug)) + ", Serbia"

    location = geolocator.geocode(
        query,
        country_codes="rs",
        language="sr",
    )

    if location:
        return location.latitude, location.longitude

    return DEFAULT_COORDS


def _connect() -> sqlite3.Connection:
    """
    Opens a short-lived connection for a single query. We deliberately do NOT
    hand out one shared, long-lived connection for the whole page render:
    Streamlit can execute reruns on a different worker thread each time, and
    a single sqlite3.Connection created on one thread can't be touched from
    another. check_same_thread=False is a backstop for the same reason.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run scrape_beer.py first, "
            f"or run generate_sample_data.py to create demo data."
        )
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def check_db_exists() -> None:
    """Call this early on each page just to raise a clean error if the db is missing."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run scrape_beer.py first, "
            f"or run generate_sample_data.py to create demo data."
        )


def get_available_dates() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scrape_date FROM prices ORDER BY scrape_date DESC"
        ).fetchall()
    return [r[0] for r in rows]


def get_latest_date() -> str | None:
    dates = get_available_dates()
    return dates[0] if dates else None


def get_leaderboard(scrape_date: str) -> pd.DataFrame:
    query = """
        SELECT p.name AS product_name, p.url AS product_url,
               pr.shop AS shop, pr.price AS price
        FROM prices pr
        JOIN products p ON p.id = pr.product_id
        WHERE pr.scrape_date = ?
    """
    with _connect() as conn:
        df = pd.read_sql_query(query, conn, params=(scrape_date,))

    if df.empty:
        return df

    min_price = df.groupby("product_name")["price"].transform("min")
    cheapest = df[df["price"] == min_price].copy()

    grouped = (
        cheapest.groupby(["product_name", "product_url", "price"])["shop"]
        .apply(lambda shops: sorted(set(shops)))
        .reset_index()
    )
    grouped["shop_display"] = grouped["shop"].apply(
        lambda shops: ", ".join(shop_display_name(s) for s in shops)
    )
    grouped["maps_url"] = grouped["shop"].apply(lambda shops: maps_search_url(shops[0]))
    grouped = grouped.sort_values("price").reset_index(drop=True)
    return grouped[["product_name", "product_url", "price", "shop_display", "maps_url"]]


def get_products() -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query("SELECT id, name, slug FROM products ORDER BY name", conn)


def get_price_history(product_id: int) -> pd.DataFrame:
    query = """
        SELECT scrape_date, shop, price
        FROM prices
        WHERE product_id = ?
        ORDER BY scrape_date
    """
    with _connect() as conn:
        df = pd.read_sql_query(query, conn, params=(product_id,))
    if not df.empty:
        df["shop_display"] = df["shop"].apply(shop_display_name)
        df["scrape_date"] = pd.to_datetime(df["scrape_date"])
    return df


def get_all_shops() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT shop FROM prices ORDER BY shop").fetchall()
    return [r[0] for r in rows]


def get_top_cheapest_for_shop(shop: str, scrape_date: str, n: int = 5) -> pd.DataFrame:
    query = """
        SELECT p.name AS product_name, pr.price AS price
        FROM prices pr
        JOIN products p ON p.id = pr.product_id
        WHERE pr.shop = ? AND pr.scrape_date = ?
        ORDER BY pr.price ASC
        LIMIT ?
    """
    with _connect() as conn:
        return pd.read_sql_query(query, conn, params=(shop, scrape_date, n))
