import streamlit as st

import rootutils

rootutils.setup_root(__file__, indicator=".gitignore", pythonpath=True)

from database.supabase_backend import SupabaseDatabase

db = SupabaseDatabase()


st.set_page_config(page_title="Beer Prices — Leaderboard", page_icon="🍺", layout="wide")

st.title("🍺 Daily Cheapest Beer Leaderboard")

try:
    db.check_connection()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

dates = db.get_available_dates()
if not dates:
    st.warning("No price data yet. Run the scraper first.")
    st.stop()

col1, col2 = st.columns([1, 3])
with col1:
    selected_date = st.selectbox("Date", dates, index=0)

leaderboard = db.get_leaderboard(selected_date)

with col2:
    search = st.text_input("Filter by beer name", "")

if search:
    leaderboard = leaderboard[
        leaderboard["product_name"].str.contains(search, case=False, na=False)
    ]

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

st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    column_config={
        "Price (RSD)": st.column_config.NumberColumn(format="%.2f RSD"),
        "Maps": st.column_config.LinkColumn("Store (Google Maps)", display_text="Open in Maps"),
    },
)

st.markdown(
    "*Note: this shows the cheapest shop **shown on the listing page** for each "
    "beer. If a shop is not listed for a beer, it may still have it in stock, but it was not found"
    "on the listing page at the time of scraping.*"
)
