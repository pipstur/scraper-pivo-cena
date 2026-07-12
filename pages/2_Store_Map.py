import plotly.graph_objects as go
import streamlit as st
from streamlit_plotly_events import plotly_events

import rootutils

rootutils.setup_root(__file__, indicator=".gitignore", pythonpath=True)

from database.supabase_backend import (
    SupabaseDatabase,
    get_shop_coords,
    shop_display_name,
    maps_search_url,
)

db = SupabaseDatabase()

st.set_page_config(
    page_title="Beer Prices — Store Map",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Store Map")

st.caption(
    "Prices in this data are per retail **chain**, not per individual branch — "
    "cenoteka.rs doesn't say which specific store had the price. "
    "Each pin represents a chain. Click a pin or choose a chain to see its "
    "cheapest beers right now."
)

try:
    db.check_connection()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()


latest_date = db.get_latest_date()

if latest_date is None:
    st.warning("No price data yet. Run the scraper first.")
    st.stop()


shops = db.get_all_shops()

if not shops:
    st.warning("No shops found.")
    st.stop()


lats = []
lons = []
names = []
slugs = []

for shop in shops:
    lat, lon = get_shop_coords(shop)

    lats.append(lat)
    lons.append(lon)
    names.append(shop_display_name(shop))
    slugs.append(shop)


col_map, col_detail = st.columns([2, 1])


with col_map:
    fig = go.Figure(
        go.Scattermapbox(
            lat=lats,
            lon=lons,
            mode="markers+text",
            marker=go.scattermapbox.Marker(
                size=16,
                color="#c0392b",
            ),
            text=names,
            textposition="top right",
            # Important:
            # this stores the database slug inside each point
            customdata=slugs,
            hovertemplate=("<b>%{text}</b>" "<extra></extra>"),
        )
    )

    fig.update_layout(
        mapbox_style="open-street-map",
        mapbox_zoom=6,
        mapbox_center={
            "lat": 44.2,
            "lon": 20.9,
        },
        margin=dict(
            l=0,
            r=0,
            t=0,
            b=0,
        ),
        height=550,
    )

    clicked = plotly_events(
        fig,
        click_event=True,
        hover_event=False,
        select_event=False,
        override_height=550,
    )


display_to_slug = dict(zip(names, slugs))


if clicked:
    # User clicked marker
    chosen_slug = clicked[0]["customdata"]
    chosen_display = db.shop_display_name(chosen_slug)

else:
    # Default / dropdown fallback
    chosen_display = st.session_state.get(
        "selected_shop",
        names[0],
    )

    chosen_slug = display_to_slug[chosen_display]

with col_detail:

    dropdown_choice = st.selectbox(
        "Choose a chain",
        names,
        index=names.index(chosen_display),
    )

    # Dropdown overrides map click
    if dropdown_choice != chosen_display:
        chosen_display = dropdown_choice
        chosen_slug = display_to_slug[chosen_display]

    st.markdown(f"### {chosen_display} — cheapest today ({latest_date})")

    top5 = db.get_top_cheapest_for_shop(
        chosen_slug,
        latest_date,
        n=10,
    )

    if top5.empty:
        st.info("No prices recorded for this chain on the latest date.")

    else:
        top5_display = top5.rename(
            columns={
                "product_name": "Beer",
                "price": "Price (RSD)",
            }
        )

        st.dataframe(
            top5_display,
            hide_index=True,
            width="stretch",
            column_config={"Price (RSD)": st.column_config.NumberColumn(format="%.2f RSD")},
        )

        st.markdown(f"[Find {chosen_display} on Google Maps]" f"({maps_search_url(chosen_slug)})")
