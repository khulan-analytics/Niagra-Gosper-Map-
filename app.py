import html
import re
from io import BytesIO
from pathlib import Path

import folium
import pandas as pd
import qrcode
import streamlit as st
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from streamlit_folium import st_folium

# ------------------------------------------------------------
# Niagara Falls Homeless Services Map
# ------------------------------------------------------------

st.set_page_config(
    page_title="Niagara Falls Homeless Services Map",
    page_icon="📍",
    layout="wide",
)

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
LOCATIONS_FILE = DATA_DIR / "locations.csv"
CACHE_FILE = DATA_DIR / "geocoded_locations.csv"

CITY_SUFFIX = "Niagara Falls, NY, USA"
MAP_CENTER = [43.0962, -79.0377]

# Double-check this matches your deployed app before printing the QR code.
APP_URL = "https://niagragospelmap.streamlit.app/"

COLORS = [
    "#C62828", "#EF6C00", "#7B1FA2", "#2E7D32",
    "#00838F", "#1565C0", "#558B2F", "#33691E",
    "#283593", "#0097A7", "#F57C00", "#6A1B9A",
    "#0D47A1", "#2E7D32", "#8E24AA", "#558B2F",
]


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

@st.cache_resource
def make_qr_png(url: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@st.cache_data
def load_locations() -> pd.DataFrame:
    """Load locations.csv.

    Required columns: id, name, street_address, phone, category
    Optional columns: latitude, longitude (manual overrides; skip geocoding)
    """
    data = pd.read_csv(LOCATIONS_FILE, dtype=str).fillna("")
    data["id"] = data["id"].astype(int)
    data["full_address"] = data["street_address"] + ", " + CITY_SUFFIX

    for col in ("latitude", "longitude"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
        else:
            data[col] = float("nan")
    return data.rename(columns={"latitude": "manual_lat", "longitude": "manual_lon"})


def load_geocode_cache() -> pd.DataFrame:
    if CACHE_FILE.exists():
        cached = pd.read_csv(CACHE_FILE)
        if {"id", "latitude", "longitude"}.issubset(cached.columns):
            cached["id"] = pd.to_numeric(cached["id"], errors="coerce")
            # Never trust old failures: keep only rows with real coordinates.
            return cached.dropna(subset=["id", "latitude", "longitude"])
    return pd.DataFrame(columns=["id", "latitude", "longitude", "matched_address"])


def save_geocode_cache(cache: pd.DataFrame) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    cache.to_csv(CACHE_FILE, index=False)


def address_variants(street: str) -> list[str]:
    """Several spellings of one address, tried in order until one geocodes."""
    short = street.replace(".", "").strip()
    long = re.sub(r"\bAve\b", "Avenue", short)
    long = re.sub(r"\bSt\b", "Street", long)
    long = re.sub(r"\bRd\b", "Road", long)
    variants = [
        f"{short}, {CITY_SUFFIX}",
        f"{long}, {CITY_SUFFIX}",
        f"{long}, Niagara Falls, New York",
        f"{long}, Niagara Falls, NY 14301",
    ]
    return list(dict.fromkeys(variants))  # dedupe, keep order


def geocode_locations(data: pd.DataFrame) -> pd.DataFrame:
    """Geocode only locations that have no cached or manual coordinates.

    - Failures are never written to the cache, so they can be retried.
    - Each id is attempted at most once per browser session to keep the
      app fast; use the sidebar button to force a retry.
    - For problem addresses, add latitude/longitude to locations.csv.
    """
    cache = load_geocode_cache()
    known_ids = set(cache["id"].astype(int))
    manual_ids = set(data.loc[data["manual_lat"].notna() & data["manual_lon"].notna(), "id"])
    attempted = st.session_state.setdefault("geocode_attempted", set())

    missing = data[~data["id"].isin(known_ids | manual_ids | attempted)]
    if missing.empty:
        return cache

    geolocator = Nominatim(
        user_agent="niagara_falls_services_map_student_project", timeout=10
    )
    geocode = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=1.1,
        max_retries=3,
        error_wait_seconds=3.0,
        swallow_exceptions=True,
    )

    progress = st.progress(0, text="Finding coordinates for service addresses...")
    new_rows = []

    for i, row in enumerate(missing.itertuples(index=False), start=1):
        result = None
        for query in address_variants(row.street_address):
            result = geocode(query, country_codes="us")
            if result:
                break

        attempted.add(int(row.id))
        if result:
            new_rows.append({
                "id": int(row.id),
                "latitude": float(result.latitude),
                "longitude": float(result.longitude),
                "matched_address": result.address,
            })
        progress.progress(i / len(missing), text=f"Geocoding address {i} of {len(missing)}...")
    progress.empty()

    if not new_rows:
        return cache

    updated = pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True)
    updated = updated.drop_duplicates(subset="id", keep="last")
    save_geocode_cache(updated)
    return updated


def make_numbered_icon(number: int, color: str) -> folium.DivIcon:
    return folium.DivIcon(
        html=f"""
        <div style="
            background:{color};
            color:white;
            border:2px solid white;
            border-radius:50%;
            width:31px;
            height:31px;
            line-height:27px;
            text-align:center;
            font-weight:800;
            font-size:14px;
            box-shadow:0 2px 6px rgba(0,0,0,.45);
        ">{number}</div>
        """,
        icon_size=(31, 31),
        icon_anchor=(15, 15),
    )


def build_map(mapped_data: pd.DataFrame) -> folium.Map:
    map_object = folium.Map(
        location=MAP_CENTER,
        zoom_start=14,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    bounds = []
    for row in mapped_data.itertuples(index=False):
        color = COLORS[(int(row.id) - 1) % len(COLORS)]
        name = html.escape(str(row.name))
        address = html.escape(str(row.full_address))
        phone = html.escape(str(row.phone)) if row.phone else "Not listed"
        category = html.escape(str(row.category))

        popup_html = f"""
        <div style="font-family:Arial; width:220px;">
          <h4 style="margin:0 0 7px 0;">{row.id}. {name}</h4>
          <div><b>Address:</b> {address}</div>
          <div><b>Phone:</b> {phone}</div>
          <div><b>Services:</b> {category}</div>
        </div>
        """

        folium.Marker(
            location=[row.latitude, row.longitude],
            tooltip=f"{row.id}. {row.name}",
            popup=folium.Popup(popup_html, max_width=300),
            icon=make_numbered_icon(row.id, color),
        ).add_to(map_object)
        bounds.append([row.latitude, row.longitude])

    if bounds:
        map_object.fit_bounds(bounds, padding=(25, 25))
    return map_object


# ------------------------------------------------------------
# Styles and header
# ------------------------------------------------------------

st.markdown(
    """
    <style>
      .main-title {font-size:2rem;font-weight:800;color:#12345a;margin-bottom:0;}
      .sub-title {color:#64748b;margin-top:0.2rem;}
      .location-card {
        border:1px solid #d9e2ec;border-radius:10px;padding:10px 12px;
        margin-bottom:8px;background:var(--secondary-background-color);
      }
      .location-name {font-weight:700;font-size:0.94rem;}
      .location-detail {font-size:0.82rem;color:inherit;margin-top:3px;}
      .pin-number {
        display:inline-flex;align-items:center;justify-content:center;
        width:25px;height:25px;border-radius:50%;color:white;
        font-weight:800;margin-right:7px;vertical-align:middle;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-title">Niagara Falls Homeless Services Map</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Support • Resources • Hope | Niagara Falls, New York</div>', unsafe_allow_html=True)
st.divider()

# ------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------

locations = load_locations()
qr_png = make_qr_png(APP_URL)

with st.sidebar:
    st.header("Map controls")

    search_text = st.text_input("Search organization, address, or service")
    show_phone = st.checkbox("Show phone numbers", value=True)

    st.caption("Map coordinates are looked up using OpenStreetMap's Nominatim service.")

    if st.button("Re-geocode all addresses", use_container_width=True):
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
        st.session_state.pop("geocode_attempted", None)
        st.rerun()

    st.divider()
    st.subheader("Access Map by QR Code")
    st.image(qr_png, caption="Scan to access Niagara Falls Services Map")
    st.download_button(
        label="Download QR Code",
        data=qr_png,
        file_name="niagara_falls_services_qr.png",
        mime="image/png",
    )

# ------------------------------------------------------------
# Data
# ------------------------------------------------------------

with st.spinner("Loading map locations..."):
    geo_cache = geocode_locations(locations)

mapped = locations.merge(geo_cache, on="id", how="left")
mapped["latitude"] = pd.to_numeric(mapped["latitude"], errors="coerce").fillna(mapped["manual_lat"])
mapped["longitude"] = pd.to_numeric(mapped["longitude"], errors="coerce").fillna(mapped["manual_lon"])
mapped["matched_address"] = mapped["matched_address"].fillna(
    "Not found. Add latitude/longitude to locations.csv"
)

if search_text.strip():
    term = search_text.strip().lower()
    haystack = (
        mapped["name"].astype(str)
        + " " + mapped["street_address"].astype(str)
        + " " + mapped["category"].astype(str)
    ).str.lower()
    filtered = mapped[haystack.str.contains(term, regex=False, na=False)]
else:
    filtered = mapped

# ------------------------------------------------------------
# Layout
# ------------------------------------------------------------

col_map, col_directory = st.columns([1.75, 1], gap="large")

with col_map:
    st.subheader("Interactive map")
    map_data = filtered.dropna(subset=["latitude", "longitude"])
    if not map_data.empty:
        st_folium(
            build_map(map_data),
            height=690,
            returned_objects=[],
            use_container_width=True,
            key="niagara_services_map",
        )
    else:
        st.warning("No mapped locations match your search.")

    mapped_count = int(mapped["latitude"].notna().sum())
    st.caption(f"{mapped_count} of {len(locations)} locations successfully mapped.")

    unmapped = mapped[mapped["latitude"].isna()]
    if not unmapped.empty:
        st.warning(
            "Not mapped: "
            + ", ".join(f"{r.id}. {r.name}" for r in unmapped.itertuples(index=False))
        )

with col_directory:
    st.subheader(f"Location directory ({len(filtered)})")
    st.caption("Numbers correspond to the numbered pins on the map.")

    for row in filtered.itertuples(index=False):
        color = COLORS[(int(row.id) - 1) % len(COLORS)]
        phone_line = (
            f'<div class="location-detail">☎ {html.escape(str(row.phone))}</div>'
            if show_phone and row.phone else ""
        )
        st.markdown(
            f"""
            <div class="location-card">
              <div class="location-name">
                <span class="pin-number" style="background:{color};">{row.id}</span>{html.escape(str(row.name))}
              </div>
              <div class="location-detail">📍 {html.escape(str(row.full_address))}</div>
              <div class="location-detail">{html.escape(str(row.category))}</div>
              {phone_line}
            </div>
            """,
            unsafe_allow_html=True,
        )

with st.expander("Address matching details"):
    st.write(
        "The geocoder's matched address is shown below so you can check that "
        "each pin points to the intended location. Rows marked 'Not found' can "
        "be fixed by adding latitude and longitude columns to locations.csv."
    )
    review = mapped[["id", "name", "full_address", "matched_address", "latitude", "longitude"]]
    st.dataframe(review, use_container_width=True, hide_index=True)

st.caption(
    "Map data © OpenStreetMap contributors. Verify locations, hours, and "
    "service details with each organization before relying on them."
)
