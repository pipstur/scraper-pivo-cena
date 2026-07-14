import streamlit as st

import rootutils

rootutils.setup_root(__file__, indicator=".gitignore", pythonpath=True)

from database.supabase_backend import SupabaseDatabase

db = SupabaseDatabase()


st.set_page_config(page_title="Beer Prices — Leaderboard", page_icon="🍺", layout="wide")

st.title("🍺 Daily Cheapest Beer Leaderboard")

try:
    db.check_connection()
except Exception as e:
    st.error(f"Couldn't connect to the database: {e}")
    st.stop()

dates = db.get_available_dates()
print(f"Available scrape dates: {dates}")
if not dates:
    st.warning("No price data yet. Run the scraper first.")
    st.stop()

col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    selected_date = st.selectbox("Date", dates, index=0)
with col2:
    rank_by = st.radio("Rank by", ["Cheapest price", "Best value (RSD/liter)"])
with col3:
    search = st.text_input("Filter by beer name", "")

by_value = rank_by == "Best value (RSD/liter)"

if by_value:
    leaderboard = db.get_best_value_leaderboard(selected_date)
else:
    leaderboard = db.get_leaderboard(selected_date)

if search and not leaderboard.empty:
    leaderboard = leaderboard[
        leaderboard["product_name"].str.contains(search, case=False, na=False)
    ]

if by_value and leaderboard.empty:
    st.info(
        "No products with a known volume for this date. Volume is parsed from the "
        "product name at scrape time -- older scrapes or unusual name formats may "
        "not have it yet."
    )
    st.stop()

if by_value:
    st.caption(
        f"{len(leaderboard)} beers with a known volume · ranked by cheapest RSD/liter "
        f"on {selected_date}. Click a store name to search it on Google Maps."
    )
    display_df = leaderboard.rename(
        columns={
            "product_name": "Beer",
            "volume_liters": "Volume (l)",
            "price": "Price (RSD)",
            "price_per_liter": "RSD / liter",
            "shop_display": "Store",
            "maps_url": "Maps",
        }
    )[["Beer", "Volume (l)", "Price (RSD)", "RSD / liter", "Store", "Maps"]]
    column_config = {
        "Volume (l)": st.column_config.NumberColumn(format="%.2f l"),
        "Price (RSD)": st.column_config.NumberColumn(format="%.2f RSD"),
        "RSD / liter": st.column_config.NumberColumn(format="%.2f RSD/l"),
        "Maps": st.column_config.LinkColumn("Store (Google Maps)", display_text="Open in Maps"),
    }
else:
    st.caption(
        f"{len(leaderboard)} beers · cheapest price found across shops on {selected_date}. "
        f"Click a store name to search it on Google Maps."
    )
    display_df = leaderboard.rename(
        columns={
            "product_name": "Beer",
            "price": "Price (RSD)",
            "shop_display": "Store",
            "maps_url": "Maps",
        }
    )[["Beer", "Price (RSD)", "Store", "Maps"]]
    column_config = {
        "Price (RSD)": st.column_config.NumberColumn(format="%.2f RSD"),
        "Maps": st.column_config.LinkColumn("Store (Google Maps)", display_text="Open in Maps"),
    }

st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    column_config=column_config,
)

st.markdown(
    "*Note: this shows the cheapest shop **shown on the listing page** for each "
    "beer. If a shop is not listed for a beer, it may still have it in stock, but it was not found"
    "on the listing page at the time of scraping.*"
)
