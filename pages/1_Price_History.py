import plotly.express as px
import streamlit as st

import rootutils

rootutils.setup_root(__file__, indicator=".gitignore", pythonpath=True)

import database.db as db

st.set_page_config(page_title="Beer Prices — History", page_icon="📈", layout="wide")

st.title("📈 Price History")

try:
    db.check_db_exists()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

products = db.get_products()
if products.empty:
    st.warning("No products in the database yet. Run the scraper first.")
    st.stop()

search = st.text_input("Search for a beer", "")
filtered = (
    products[products["name"].str.contains(search, case=False, na=False)] if search else products
)

if filtered.empty:
    st.info("No beers match that search.")
    st.stop()

options = dict(zip(filtered["name"], filtered["id"]))
chosen_name = st.selectbox("Select a beer", list(options.keys()))
product_id = int(options[chosen_name])

history = db.get_price_history(product_id)

if history.empty:
    st.info("No price history recorded yet for this beer.")
    st.stop()

history_display = history.rename(columns={"shop_display": "Store"})

fig = px.line(
    history_display,
    x="scrape_date",
    y="price",
    color="Store",
    markers=True,
    labels={"scrape_date": "Date", "price": "Price (RSD)"},
    title=f"Price over time — {chosen_name}",
)
fig.update_layout(hovermode="x unified")
st.plotly_chart(fig, width="stretch")

st.subheader("Stats per store")
stats = (
    history.groupby("shop_display")["price"]
    .agg(["min", "max", "mean", "count"])
    .rename(columns={"min": "Lowest", "max": "Highest", "mean": "Average", "count": "Days seen"})
    .round(2)
    .sort_values("Lowest")
)
st.dataframe(stats, width="stretch")
