"""
Supabase data access layer for the beer price dashboard.
"""

from functools import lru_cache
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st

from geopy.geocoders import Nominatim
from supabase import Client, create_client


geolocator = Nominatim(user_agent="beer-price-dashboard")

pd.set_option("future.infer_string", False)


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


class SupabaseDatabase:
    """Handles all reads from the Supabase database."""

    def __init__(self) -> None:
        self.client: Client = create_client(
            st.secrets["SUPABASE_URL"],
            st.secrets["SUPABASE_SERVICE_ROLE_KEY"],
        )

    def check_connection(self) -> None:
        """Verify that Supabase is reachable."""
        self.client.table("products").select("id").limit(1).execute()

    def get_available_dates(self) -> list[str]:
        """Return all available scrape dates."""

        response = (
            self.client.table("prices")
            .select("scrape_date")
            .order(
                "scrape_date",
                desc=True,
            )
            .execute()
        )

        return sorted(
            {row["scrape_date"] for row in response.data},
            reverse=True,
        )

    def get_latest_date(self) -> str | None:
        """Return newest scrape date."""
        dates = self.get_available_dates()
        return dates[0] if dates else None

    def get_leaderboard(
        self,
        scrape_date: str,
    ) -> pd.DataFrame:
        """Return cheapest beer prices across shops."""

        response = (
            self.client.table("prices")
            .select(
                """
                price,
                shop,
                products(
                    name,
                    url
                )
                """
            )
            .eq(
                "scrape_date",
                scrape_date,
            )
            .execute()
        )

        rows = [
            {
                "product_name": row["products"]["name"],
                "product_url": row["products"]["url"],
                "shop": row["shop"],
                "price": row["price"],
            }
            for row in response.data
        ]

        df = pd.DataFrame(rows)

        if df.empty:
            return df

        min_price = df.groupby("product_name")["price"].transform("min")

        cheapest = df[df["price"] == min_price].copy()

        grouped = (
            cheapest.groupby(
                [
                    "product_name",
                    "product_url",
                    "price",
                ]
            )["shop"]
            .apply(lambda shops: sorted(set(shops)))
            .reset_index()
        )

        grouped["shop_display"] = grouped["shop"].apply(
            lambda shops: ", ".join(shop_display_name(shop) for shop in shops)
        )

        grouped["maps_url"] = grouped["shop"].apply(lambda shops: maps_search_url(shops[0]))

        grouped = grouped.sort_values("price").reset_index(drop=True)

        return grouped[
            [
                "product_name",
                "product_url",
                "price",
                "shop_display",
                "maps_url",
            ]
        ]

    def get_best_value_leaderboard(
        self,
        scrape_date: str,
    ) -> pd.DataFrame:
        """Return cheapest price-per-liter across shops.

        Only includes products where volume_liters is known (parsed from the
        product name at scrape time) -- a product with no detectable volume
        can't be compared on a per-liter basis, so it's silently excluded
        rather than shown with a misleading price-per-liter of the raw price.
        """

        response = (
            self.client.table("prices")
            .select(
                """
                price,
                shop,
                products(
                    name,
                    url,
                    volume_liters
                )
                """
            )
            .eq(
                "scrape_date",
                scrape_date,
            )
            .execute()
        )

        rows = [
            {
                "product_name": row["products"]["name"],
                "product_url": row["products"]["url"],
                "volume_liters": row["products"]["volume_liters"],
                "shop": row["shop"],
                "price": row["price"],
            }
            for row in response.data
            if row["products"]["volume_liters"]
        ]

        df = pd.DataFrame(rows)

        if df.empty:
            return df

        df["price_per_liter"] = df["price"] / df["volume_liters"]

        min_ppl = df.groupby("product_name")["price_per_liter"].transform("min")

        cheapest = df[df["price_per_liter"] == min_ppl].copy()

        grouped = (
            cheapest.groupby(
                [
                    "product_name",
                    "product_url",
                    "volume_liters",
                    "price",
                    "price_per_liter",
                ]
            )["shop"]
            .apply(lambda shops: sorted(set(shops)))
            .reset_index()
        )

        grouped["shop_display"] = grouped["shop"].apply(
            lambda shops: ", ".join(shop_display_name(shop) for shop in shops)
        )

        grouped["maps_url"] = grouped["shop"].apply(lambda shops: maps_search_url(shops[0]))

        grouped = grouped.sort_values("price_per_liter").reset_index(drop=True)

        return grouped[
            [
                "product_name",
                "product_url",
                "volume_liters",
                "price",
                "price_per_liter",
                "shop_display",
                "maps_url",
            ]
        ]

    def get_products(self) -> pd.DataFrame:
        """Return all products."""

        response = self.client.table("products").select("id,name,slug").order("name").execute()

        return pd.DataFrame(response.data)

    def get_price_history(
        self,
        product_id: int,
    ) -> pd.DataFrame:
        """Return historical prices for a product."""

        response = (
            self.client.table("prices")
            .select("scrape_date,shop,price")
            .eq(
                "product_id",
                product_id,
            )
            .order("scrape_date")
            .execute()
        )

        df = pd.DataFrame(response.data)

        if not df.empty:
            df["shop_display"] = df["shop"].apply(shop_display_name)
            df["scrape_date"] = pd.to_datetime(df["scrape_date"])

        return df

    def get_all_shops(self) -> list[str]:
        """Return all shops present in prices."""

        response = self.client.table("prices").select("shop").execute()

        return sorted({row["shop"] for row in response.data})

    def get_top_cheapest_for_shop(
        self,
        shop: str,
        scrape_date: str,
        n: int = 5,
    ) -> pd.DataFrame:
        """Return cheapest beers for a shop on a date."""

        response = (
            self.client.table("prices")
            .select(
                """
                price,
                products(name)
                """
            )
            .eq(
                "shop",
                shop,
            )
            .eq(
                "scrape_date",
                scrape_date,
            )
            .order("price")
            .limit(n)
            .execute()
        )

        return pd.DataFrame(
            [
                {
                    "product_name": row["products"]["name"],
                    "price": row["price"],
                }
                for row in response.data
            ]
        )
