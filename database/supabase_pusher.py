"""
Push local SQLite scraper data to Supabase.

Reads products and prices from a SQLite database and synchronizes them
with the Supabase PostgreSQL database.
"""

import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client
import os


load_dotenv()


class SupabasePusher:
    """Synchronizes scraper data from SQLite to Supabase."""

    def __init__(self, sqlite_path: str):
        self.sqlite_path = Path(sqlite_path)

        self.client: Client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )

    def push(self) -> None:
        """Push all local data to Supabase."""
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row

            products = self._fetch_products(conn)
            product_map = self._push_products(products)

            prices = self._fetch_prices(conn)
            self._push_prices(prices, product_map)

    def _fetch_products(self, conn: sqlite3.Connection) -> list[dict]:
        """Fetch products from SQLite."""
        cursor = conn.execute(
            """
            SELECT id, slug, name, url
            FROM products
            """
        )

        return [dict(row) for row in cursor.fetchall()]

    def _fetch_prices(self, conn: sqlite3.Connection) -> list[dict]:
        """Fetch prices from SQLite."""
        cursor = conn.execute(
            """
            SELECT
                product_id,
                shop,
                price,
                scrape_date,
                scraped_at
            FROM prices
            """
        )

        return [dict(row) for row in cursor.fetchall()]

    def _push_products(self, products: list[dict]) -> dict[int, int]:
        """
        Push products and return SQLite ID -> Supabase ID mapping.
        """

        if not products:
            return {}

        response = (
            self.client.table("products")
            .upsert(
                [
                    {
                        "slug": product["slug"],
                        "name": product["name"],
                        "url": product["url"],
                    }
                    for product in products
                ],
                on_conflict="slug",
            )
            .execute()
        )

        supabase_products = response.data

        slug_to_supabase_id = {product["slug"]: product["id"] for product in supabase_products}

        return {product["id"]: slug_to_supabase_id[product["slug"]] for product in products}

    def _push_prices(
        self,
        prices: list[dict],
        product_map: dict[int, int],
    ) -> None:
        """Push prices to Supabase."""

        if not prices:
            return

        payload = []

        for price in prices:
            payload.append(
                {
                    "product_id": product_map[price["product_id"]],
                    "shop": price["shop"],
                    "price": price["price"],
                    "scrape_date": price["scrape_date"],
                    "scraped_at": price["scraped_at"],
                }
            )

        (
            self.client.table("prices")
            .upsert(
                payload,
                on_conflict="product_id,shop,scrape_date",
            )
            .execute()
        )


def push_sqlite_to_supabase(sqlite_path: str) -> None:
    """
    Synchronize SQLite scraper database with Supabase.

    Args:
        sqlite_path: Path to local SQLite database.
    """
    pusher = SupabasePusher(sqlite_path)
    pusher.push()


if __name__ == "__main__":
    push_sqlite_to_supabase("database/beer_prices.db")
