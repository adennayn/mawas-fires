"""Streamlit dashboard: VIIRS/MODIS fire hotspots in the Mawas landscape and its villages."""

from datetime import timedelta

import folium
import pandas as pd
import streamlit as st
from branca.element import MacroElement
from jinja2 import Template
from streamlit_folium import st_folium

from exports import to_csv_bytes, to_excel_bytes, to_kml_bytes
from fetch_firms import EARLIEST_DATE, SENSORS, fetch_hotspots, load_aois

WIB = "Asia/Jakarta"  # Western Indonesian Time, UTC+7
PRESETS = {"Last 24 hours": 1, "Last 3 days": 3, "Last 7 days": 7, "Last 30 days": 30}
CUSTOM = "Custom dates"
MAX_CUSTOM_DAYS = 31
ALL_AOIS = "All AOIs"

# Age classes, measured back from the end of the selected period
AGE_24H, AGE_72H, AGE_OLDER = "Last 24 hours", "24–72 hours", "Older than 72 hours"
AGE_COLORS = {AGE_24H: "#e31a1c", AGE_72H: "#ff7f00", AGE_OLDER: "#ffd400"}
AGE_OUTLINES = {AGE_24H: "#a50f15", AGE_72H: "#b35900", AGE_OLDER: "#8a7300"}
LANDSCAPE_COLOR = "#2e7d32"
VILLAGE_COLOR = "#1f5fbf"

DAYNIGHT_LABELS = {"D": "Day", "N": "Night"}

# Pulsing marker for the last 24 hours, plus legend styling
MAP_CSS = f"""
<style>
.pulse-icon {{ background: none; border: none; }}
.pulse-dot {{
  position: relative; width: 100%; height: 100%; border-radius: 50%;
  background: {AGE_COLORS[AGE_24H]}; opacity: 0.9;
  border: 1px solid {AGE_OUTLINES[AGE_24H]}; box-sizing: border-box;
}}
.pulse-dot::after {{
  content: ""; position: absolute; inset: -1px; border-radius: 50%;
  background: {AGE_COLORS[AGE_24H]}; animation: firms-pulse 1.6s ease-out infinite;
}}
@keyframes firms-pulse {{
  0%   {{ transform: scale(1);   opacity: 0.8; }}
  100% {{ transform: scale(2.8); opacity: 0; }}
}}
.map-legend {{
  background: rgba(255, 255, 255, 0.92); padding: 8px 10px; border-radius: 6px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3); font: 12px/1.5 system-ui, sans-serif; color: #222;
}}
.map-legend b {{ display: block; margin: 4px 0 2px; }}
.map-legend .lg-row {{ display: flex; align-items: center; gap: 6px; }}
.map-legend .lg-dot {{ display: inline-block; flex: none; width: 12px; min-width: 12px; height: 12px; border-radius: 50%; box-sizing: border-box; }}
.map-legend .lg-line {{ flex: none; width: 18px; height: 0; }}
</style>
"""


class Legend(MacroElement):
    """Static HTML legend added as a Leaflet control in the map corner."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        var legend = L.control({position: "bottomright"});
        legend.onAdd = function () {
            var div = L.DomUtil.create("div", "map-legend");
            div.innerHTML = {{ this.html|tojson }};
            return div;
        };
        legend.addTo({{ this._parent.get_name() }});
        {% endmacro %}
    """)

    def __init__(self, html: str):
        super().__init__()
        self._name = "Legend"
        self.html = html


st.set_page_config(page_title="Mawas Fire Early Warning", layout="wide")


@st.cache_data(ttl=3600)
def get_aois():
    return load_aois()


@st.cache_data(ttl=900, show_spinner="Fetching hotspots from NASA FIRMS…")
def get_hotspots(start, end, sensors, _aois):
    return fetch_hotspots(start, end, _aois, sensors)


def frp_radius(frp: float) -> float:
    # Scale radius with FRP (MW), clamped so tiny and huge fires stay readable
    return max(4, min(25, 3 + frp ** 0.5 * 2))


def age_class(acq: pd.Series, ref: pd.Timestamp) -> pd.Series:
    age = ref - acq
    return pd.Series(
        pd.cut(
            age,
            bins=[pd.Timedelta(days=-1), pd.Timedelta(hours=24), pd.Timedelta(hours=72), pd.Timedelta.max],
            labels=[AGE_24H, AGE_72H, AGE_OLDER],
            right=False,
        ).astype(str),
        index=acq.index,
    )


def build_table(df: pd.DataFrame) -> pd.DataFrame:
    wib = df["acq_datetime"].dt.tz_convert(WIB)
    return pd.DataFrame({
        "Date (WIB)": wib.dt.strftime("%Y-%m-%d"),
        "Time (WIB)": wib.dt.strftime("%H:%M"),
        "Age": df["age"],
        "Confidence": df["confidence_class"].str.title(),
        "Latitude (y)": df["latitude"],
        "Longitude (x)": df["longitude"],
        "FRP (MW)": df["frp"],
        "Brightness (K)": df["brightness_k"],
        "AOI": df["aoi"].map("; ".join),
        "Satellite": df["satellite_name"],
        "Sensor": df["instrument"],
        "Day/Night": df["daynight"].map(DAYNIGHT_LABELS).fillna(df["daynight"]),
        "Acquired (UTC)": df["acq_datetime"].dt.strftime("%Y-%m-%d %H:%M"),
        # VIIRS: l/n/h; MODIS: 0-100 %
        "Raw confidence": df["confidence"].astype(str),
        "FIRMS source": df["source"],
    }).reset_index(drop=True)


def legend_html(counts: pd.Series, selected: str) -> str:
    def dot(age):
        return (f'<div class="lg-row"><span class="lg-dot" style="background:{AGE_COLORS[age]};'
                f'border:1px solid {AGE_OUTLINES[age]}"></span>{age} ({counts.get(age, 0)})</div>')

    def line(color, label, style="solid"):
        return (f'<div class="lg-row"><span class="lg-line" style="border-top:3px {style} {color}">'
                f"</span>{label}</div>")

    return (
        "<b>Hotspot age</b>"
        + "".join(dot(a) for a in AGE_COLORS)
        + '<div style="color:#555">Red points pulse</div>'
        + '<div style="color:#555">Larger circle = higher fire intensity<br>(Fire Radiative Power, MW)</div>'
        + "<b>Boundaries</b>"
        + line(LANDSCAPE_COLOR, "Mawas landscape")
        + line(VILLAGE_COLOR, "Village", "dashed")
        + ("" if selected == ALL_AOIS else line("#111", f"Selected: {selected}"))
    )


# Soft chips for multiselects instead of the theme's red, which reads like a fire alert.
# Translucent so it works in both light and dark mode.
st.html("""
<style>
[data-testid="stMultiSelect"] span[data-tag] {
  background-color: rgba(31, 95, 191, 0.14) !important;
  color: inherit !important;
  border: 1px solid rgba(31, 95, 191, 0.35);
}
[data-testid="stMultiSelect"] span[data-tag] svg { fill: currentColor; opacity: 0.6; }
</style>
""")

st.title("Mawas Fire Early Warning Platform")
st.caption(
    "VIIRS and MODIS active fire detections (nominal & high confidence) from NASA FIRMS, "
    "for the Mawas landscape and surrounding villages in Central Kalimantan. Times in WIB (UTC+7)."
)

aois = get_aois()
now = pd.Timestamp.now(tz=WIB)

map_col, side_col = st.columns([2.4, 1], gap="medium")

with side_col:
    metric_24h, metric_total = st.columns(2)

    # --- Date range ------------------------------------------------------------
    with st.container(border=True):
        st.markdown("**Date range & satellites**")
        choice = st.selectbox("Period", [*PRESETS, CUSTOM], index=0)
        if choice == CUSTOM:
            picked = st.date_input(
                "Start and end date (WIB)",
                value=(now.date() - timedelta(days=6), now.date()),
                min_value=EARLIEST_DATE,
                max_value=now.date(),
                format="DD/MM/YYYY",
            )
            if len(picked) != 2:
                st.info("Pick an end date to load data.")
                st.stop()
            if (picked[1] - picked[0]).days + 1 > MAX_CUSTOM_DAYS:
                st.error(f"Please choose at most {MAX_CUSTOM_DAYS} days.")
                st.stop()
            window_start = pd.Timestamp(picked[0], tz=WIB)
            window_end = min(now, pd.Timestamp(picked[1], tz=WIB) + timedelta(days=1))
        else:
            window_end = now
            window_start = now - timedelta(days=PRESETS[choice])
        st.caption(f"{window_start:%d %b %Y %H:%M} – {window_end:%d %b %Y %H:%M} WIB")
        picked_sensors = st.multiselect(
            "Satellites",
            list(SENSORS),
            default=list(SENSORS),
            help="VIIRS (375 m pixels) on Suomi NPP, NOAA-20 and NOAA-21; MODIS (1 km pixels) "
            "on Terra and Aqua. More satellites mean more overpasses per day.",
        )
        if not picked_sensors:
            st.info("Select at least one satellite.")
            st.stop()
        sensors = tuple(s for s in SENSORS if s in picked_sensors)

    # --- AOI selection ---------------------------------------------------------
    with st.container(border=True):
        st.markdown("**Area of interest**")
        selected = st.selectbox(
            "AOI", [ALL_AOIS, *aois["name"]], help="Filter hotspots and zoom the map to one area."
        )
        aoi_info = st.empty()
        aoi_counts = st.empty()

# FIRMS dates are UTC, so fetch every UTC date the WIB window touches
fetch_start = window_start.tz_convert("UTC").date()
fetch_end = window_end.tz_convert("UTC").date()
try:
    hotspots = get_hotspots(fetch_start, fetch_end, sensors, aois)
except Exception as exc:
    st.error(f"Could not load FIRMS data: {exc}")
    st.stop()

hotspots = hotspots[
    (hotspots["acq_datetime"] >= window_start) & (hotspots["acq_datetime"] < window_end)
].copy()
hotspots["age"] = age_class(hotspots["acq_datetime"], window_end)

if selected == ALL_AOIS:
    shown = hotspots
    focus = aois
else:
    shown = hotspots[hotspots["aoi"].map(lambda names: selected in names)]
    focus = aois[aois["name"] == selected]

# --- Side panel: metrics and AOI summary ----------------------------------------
is_current = choice != CUSTOM or window_end == now
metric_24h.metric(
    "Last 24 hrs in AOI" if is_current else "Final 24 hrs in AOI",
    int((shown["age"] == AGE_24H).sum()),
    border=True,
    help="Hotspots detected in the 24 hours before now"
    if is_current else "Hotspots detected in the last 24 hours of the selected period.",
)
metric_total.metric("Total hotspots in AOI", len(shown), border=True, help="Within the selected period.")

if selected == ALL_AOIS:
    aoi_info.caption(f"Mawas landscape and {len(aois) - 1} villages")
else:
    row = focus.iloc[0]
    where = "Landscape" if row["kind"] == "landscape" else f"Kec. {row['district']}, Kab. {row['regency']}"
    aoi_info.caption(f"{where} · {row['area_ha']:,.0f} ha")

per_aoi = hotspots.explode("aoi").groupby("aoi").agg(
    Total=("age", "size"), **{"24 hrs": ("age", lambda a: (a == AGE_24H).sum())}
)
summary = (
    aois[["name"]].rename(columns={"name": "AOI"}).set_index("AOI")
    .join(per_aoi).fillna(0).astype(int).reset_index()
)
aoi_counts.dataframe(summary, hide_index=True, width="stretch")

# --- Map ------------------------------------------------------------------------
with map_col:
    minx, miny, maxx, maxy = focus.total_bounds
    m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2], zoom_start=10, tiles=None)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics",
        name="Satellite imagery (Esri)",
        show=False,
    ).add_to(m)
    m.get_root().header.add_child(folium.Element(MAP_CSS))

    boundaries = folium.FeatureGroup(name="AOI boundaries").add_to(m)
    for _, aoi in aois.iterrows():
        is_selected = aoi["name"] == selected
        is_landscape = aoi["kind"] == "landscape"
        color = LANDSCAPE_COLOR if is_landscape else VILLAGE_COLOR
        folium.GeoJson(
            aoi["geometry"].__geo_interface__,
            tooltip=aoi["name"],
            style_function=lambda _, c=color, sel=is_selected, land=is_landscape: {
                "color": "#111" if sel else c,
                "weight": 4 if sel else 2,
                "dashArray": None if land or sel else "6 4",
                "fillColor": c,
                "fillOpacity": 0.15 if sel else 0.04,
            },
        ).add_to(boundaries)

    def popup(row) -> folium.Popup:
        wib = row["acq_datetime"].tz_convert(WIB)
        return folium.Popup(
            f"<b>{wib:%d %b %Y %H:%M} WIB</b><br>"
            f"Satellite: {row['satellite_name']} ({row['instrument']})<br>"
            f"Confidence: {row['confidence_class'].title()}<br>"
            f"FRP: {row['frp']:.1f} MW<br>"
            f"{row['latitude']:.5f}, {row['longitude']:.5f}<br>"
            f"AOI: {', '.join(row['aoi'])}",
            max_width=240,
        )

    points = folium.FeatureGroup(name="Hotspots").add_to(m)
    # Oldest first, so the most recent points sit on top
    for age in (AGE_OLDER, AGE_72H):
        for _, row in shown[shown["age"] == age].iterrows():
            folium.CircleMarker(
                location=[row["latitude"], row["longitude"]],
                radius=frp_radius(row["frp"]),
                color=AGE_OUTLINES[age],
                weight=1,
                fill=True,
                fill_color=AGE_COLORS[age],
                fill_opacity=0.85,
                popup=popup(row),
            ).add_to(points)
    for _, row in shown[shown["age"] == AGE_24H].iterrows():
        size = round(frp_radius(row["frp"]) * 2)
        folium.Marker(
            location=[row["latitude"], row["longitude"]],
            icon=folium.DivIcon(
                html='<div class="pulse-dot"></div>',
                icon_size=(size, size),
                icon_anchor=(size // 2, size // 2),
                class_name="pulse-icon",
            ),
            popup=popup(row),
        ).add_to(points)

    m.add_child(Legend(legend_html(shown["age"].value_counts(), selected)))
    folium.LayerControl(collapsed=True).add_to(m)
    m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))
    # Key on the AOI so the map re-zooms whenever the selection changes
    st_folium(m, key=f"map-{selected}", use_container_width=True, height=780, returned_objects=[])

# --- Table and downloads --------------------------------------------------------
with st.container(border=True):
    st.subheader(f"Hotspot detections · {selected}")
    if shown.empty:
        st.info("No nominal/high-confidence hotspots in this AOI for the selected period.")
    else:
        full_table = build_table(shown)

        # Filters narrow the table and downloads; the map keeps showing every detection
        f_age, f_conf, f_sat, f_dn, f_frp = st.columns([1.3, 1, 1.3, 1, 0.9])
        filters = {
            "Age": f_age.multiselect("Age", [a for a in AGE_COLORS if a in set(full_table["Age"])], placeholder="All"),
            "Confidence": f_conf.multiselect("Confidence", sorted(full_table["Confidence"].unique()), placeholder="All"),
            "Satellite": f_sat.multiselect("Satellite", sorted(full_table["Satellite"].unique()), placeholder="All"),
            "Day/Night": f_dn.multiselect("Day/Night", sorted(full_table["Day/Night"].unique()), placeholder="All"),
        }
        min_frp = f_frp.number_input("Min FRP (MW)", min_value=0.0, value=0.0, step=1.0)
        mask = full_table["FRP (MW)"] >= min_frp
        for col, values in filters.items():
            if values:
                mask &= full_table[col].isin(values)
        table = full_table[mask].reset_index(drop=True)
        filter_desc = [f"{k}: {', '.join(v)}" for k, v in filters.items() if v]
        if min_frp:
            filter_desc.append(f"Min FRP: {min_frp:g} MW")
        st.caption(
            f"Showing {len(table):,} of {len(full_table):,} detections. "
            "Filters apply to the table and downloads, not the map."
        )
        if table.empty:
            st.info("No detections match these filters.")
            st.stop()

        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            height=min(420, 38 + 35 * len(table)),
            column_config={
                "Latitude (y)": st.column_config.NumberColumn(format="%.5f"),
                "Longitude (x)": st.column_config.NumberColumn(format="%.5f"),
                "FRP (MW)": st.column_config.NumberColumn(
                    format="%.2f", help="Fire Radiative Power: heat released by the fire, in megawatts"
                ),
                "Brightness (K)": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        slug = selected.lower().replace(" ", "_")
        stem = f"hotspots_{slug}_{window_start:%Y%m%d}-{window_end:%Y%m%d}"
        period = f"{window_start:%d %b %Y %H:%M} – {window_end:%d %b %Y %H:%M} WIB"
        info = {
            "AOI": selected,
            "Period": period,
            "Detections": str(len(table)),
            "Data": f"NASA FIRMS {', '.join(sensors)} (NRT / SP), nominal & high confidence",
            "Table filters": "; ".join(filter_desc) or "None",
            "Generated": f"{now:%d %b %Y %H:%M} WIB",
        }
        d1, d2, d3 = st.columns(3)
        d1.download_button(
            "Download Excel (.xlsx)",
            to_excel_bytes(table, info),
            file_name=f"{stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
        d2.download_button(
            "Download CSV", to_csv_bytes(table), file_name=f"{stem}.csv", mime="text/csv", width="stretch"
        )
        d3.download_button(
            "Download KML (Google Earth / GIS)",
            to_kml_bytes(table, f"FIRMS hotspots · {selected} · {period}"),
            file_name=f"{stem}.kml",
            mime="application/vnd.google-earth.kml+xml",
            width="stretch",
        )
