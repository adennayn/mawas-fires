"""Fetch VIIRS and MODIS hotspots from the NASA FIRMS Area API and clip them to the project AOIs."""

import io
import os
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
AOI_DIR = BASE_DIR / "aois"
LANDSCAPE_FILE = "mawas.geojson"
LANDSCAPE_NAME = "Mawas"

API_ROOT = "https://firms.modaps.eosdis.nasa.gov/api"
# Satellite -> (near-real-time source, standard-processing archive source).
# Recent dates come from NRT, older dates from SP. NOAA-21 has no SP archive yet;
# its NRT feed reaches back to its first data (Jan 2024).
SENSORS = {
    "VIIRS S-NPP": ("VIIRS_SNPP_NRT", "VIIRS_SNPP_SP"),
    "VIIRS NOAA-20": ("VIIRS_NOAA20_NRT", "VIIRS_NOAA20_SP"),
    "VIIRS NOAA-21": ("VIIRS_NOAA21_NRT", None),
    "MODIS Terra/Aqua": ("MODIS_NRT", "MODIS_SP"),
}
SATELLITE_NAMES = {"N": "Suomi NPP", "N20": "NOAA-20", "N21": "NOAA-21", "T": "Terra", "A": "Aqua"}
# Central Kalimantan: west, south, east, north
BBOX = (111.0, -4.0, 116.0, 0.0)
DAY_RANGE = 3
MAX_DAYS_PER_REQUEST = 5  # FIRMS Area API limit
VIIRS_CONFIDENCE = {"l": "low", "n": "nominal", "h": "high"}
KEEP_CONFIDENCE = {"nominal", "high"}
# Earliest record across the sources above (MODIS Terra)
EARLIEST_DATE = date(2000, 11, 1)
# Metric CRS for area calculations (UTM 50S covers Mawas)
AREA_CRS = "EPSG:32750"


def get_map_key() -> str:
    load_dotenv(BASE_DIR / ".env")
    map_key = os.getenv("FIRMS_MAP_KEY")
    if not map_key:
        raise RuntimeError("FIRMS_MAP_KEY is not set in .env")
    return map_key


def load_aois(aoi_dir: Path = AOI_DIR) -> gpd.GeoDataFrame:
    """Load every AOI in aoi_dir as one row each, in WGS84.

    Columns: name, kind ("landscape" or "village"), district, regency, area_ha, geometry.
    Mawas comes first, then villages alphabetically. Village names come from the
    NAMOBJ attribute (Indonesian village boundary data), falling back to the file name.
    """
    rows = []
    for path in sorted(aoi_dir.glob("*.geojson")):
        gdf = gpd.read_file(path).to_crs("EPSG:4326")
        attrs = gdf.iloc[0]
        is_landscape = path.name == LANDSCAPE_FILE
        if is_landscape:
            name = LANDSCAPE_NAME
        else:
            name = attrs.get("NAMOBJ") or path.stem.replace("_", " ").title()
        rows.append({
            "name": name,
            "kind": "landscape" if is_landscape else "village",
            "district": None if is_landscape else attrs.get("WADMKC"),
            "regency": None if is_landscape else attrs.get("WADMKK"),
            "geometry": gdf.union_all(),
        })
    aois = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    aois["area_ha"] = aois.to_crs(AREA_CRS).area / 10_000
    aois["_is_village"] = aois["kind"] == "village"
    return aois.sort_values(["_is_village", "name"]).drop(columns="_is_village").reset_index(drop=True)


def get_availability(map_key: str) -> dict[str, tuple[date, date]]:
    """Return {source: (min_date, max_date)} for every FIRMS source."""
    resp = requests.get(f"{API_ROOT}/data_availability/csv/{map_key}/ALL", timeout=60)
    if not resp.ok:
        raise RuntimeError(f"FIRMS availability error (HTTP {resp.status_code}): {resp.text[:200]}")
    df = pd.read_csv(io.StringIO(resp.text))
    return {
        row.data_id: (date.fromisoformat(row.min_date), date.fromisoformat(row.max_date))
        for row in df.itertuples()
    }


def plan_requests(
    start: date, end: date, sensor: str, availability: dict[str, tuple[date, date]]
) -> list[tuple[str, date, int]]:
    """Split [start, end] into (source, start_date, day_count) requests for one sensor.

    Each request covers at most MAX_DAYS_PER_REQUEST days. NRT takes precedence where
    both feeds cover a day, and dates before a source's first record are skipped
    (FIRMS rejects them).
    """
    nrt, sp = SENSORS[sensor]
    requests_ = []
    nrt_min = availability.get(nrt, (end + timedelta(days=1), end))[0]
    segments = [(nrt, max(start, nrt_min), end)]
    if sp in availability:
        sp_min, sp_max = availability[sp]
        segments.insert(0, (sp, max(start, sp_min), min(end, sp_max, nrt_min - timedelta(days=1))))
    for source, seg_start, seg_end in segments:
        day = seg_start
        while day <= seg_end:
            days = min(MAX_DAYS_PER_REQUEST, (seg_end - day).days + 1)
            requests_.append((source, day, days))
            day += timedelta(days=days)
    return requests_


def fetch_raw(map_key: str, source: str, start: date, days: int) -> pd.DataFrame:
    bbox = ",".join(str(v) for v in BBOX)
    url = f"{API_ROOT}/area/csv/{map_key}/{source}/{bbox}/{days}/{start.isoformat()}"
    resp = requests.get(url, timeout=120)
    text = resp.text.strip()
    # FIRMS returns plain-text errors (e.g. "Invalid MAP_KEY."), sometimes with HTTP 200.
    # Report the body rather than raise_for_status(), whose message would expose the key in the URL.
    if not resp.ok or not text.lower().startswith("latitude"):
        raise RuntimeError(f"FIRMS API error ({source}, HTTP {resp.status_code}): {text[:200]}")
    df = pd.read_csv(io.StringIO(text))
    df["source"] = source
    return df


def confidence_class(confidence: pd.Series) -> pd.Series:
    """Normalise confidence to low/nominal/high.

    VIIRS reports l/n/h; MODIS reports 0-100 %, classed per FIRMS as
    low < 30, nominal 30-79, high >= 80.
    """
    viirs = confidence.astype(str).str.lower().map(VIIRS_CONFIDENCE)
    pct = pd.to_numeric(confidence, errors="coerce")
    modis = pd.cut(pct, bins=[-1, 29, 79, 100], labels=["low", "nominal", "high"]).astype(object)
    return viirs.fillna(modis)


HOTSPOT_COLUMNS = [
    "latitude", "longitude", "acq_date", "acq_time", "satellite", "satellite_name", "instrument",
    "sensor", "confidence", "confidence_class", "frp", "brightness_k", "daynight", "source",
    "aoi", "acq_datetime", "geometry",
]


def fetch_hotspots(
    start: date | None = None,
    end: date | None = None,
    aois: gpd.GeoDataFrame | None = None,
    sensors: tuple[str, ...] = tuple(SENSORS),
) -> gpd.GeoDataFrame:
    """Return nominal/high-confidence hotspots inside any AOI for [start, end] (UTC dates).

    Defaults to the last DAY_RANGE days, all AOIs in aois/ and all sensors. The "aoi"
    column lists the names of every AOI containing the point (villages can overlap Mawas).
    """
    end = end or pd.Timestamp.now(tz="UTC").date()
    start = start or end - timedelta(days=DAY_RANGE - 1)
    if start > end:
        raise ValueError("start date must be on or before end date")
    if aois is None:
        aois = load_aois()

    map_key = get_map_key()
    availability = get_availability(map_key)
    frames = []
    for sensor in sensors:
        for source, day, days in plan_requests(start, end, sensor, availability):
            df = fetch_raw(map_key, source, day, days)
            if not df.empty:
                df["sensor"] = sensor
                frames.append(df)
    if not frames:
        return gpd.GeoDataFrame(columns=HOTSPOT_COLUMNS, geometry="geometry", crs="EPSG:4326")
    df = pd.concat(frames, ignore_index=True)
    df["confidence_class"] = confidence_class(df["confidence"])
    df = df[df["confidence_class"].isin(KEEP_CONFIDENCE)]
    df = df.drop_duplicates(subset=["latitude", "longitude", "acq_date", "acq_time", "satellite"])
    # VIIRS I-4 band or MODIS band 21/22 brightness temperature, whichever the sensor has
    empty = pd.Series(index=df.index, dtype=float)
    df["brightness_k"] = df.get("bright_ti4", empty).fillna(df.get("brightness", empty))
    df["satellite_name"] = df["satellite"].astype(str).map(SATELLITE_NAMES).fillna(df["satellite"])

    points = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, aois[["name", "geometry"]], how="inner", predicate="within")
    names = joined.groupby(level=0)["name"].agg(list)
    inside = points.loc[names.index].copy()
    inside["aoi"] = names

    # acq_time is HHMM in UTC, stored as int (e.g. 545 -> 05:45)
    inside["acq_time"] = inside["acq_time"].astype(int).astype(str).str.zfill(4)
    inside["acq_datetime"] = pd.to_datetime(
        inside["acq_date"] + " " + inside["acq_time"], format="%Y-%m-%d %H%M", utc=True
    )
    inside = inside[HOTSPOT_COLUMNS]
    return inside.sort_values("acq_datetime", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    gdf = fetch_hotspots()
    print(f"{len(gdf)} hotspots in AOIs (last {DAY_RANGE} days)")
    if not gdf.empty:
        print(gdf.explode("aoi")["aoi"].value_counts())
        print(gdf["satellite_name"].value_counts())
        print(gdf[["acq_date", "acq_time", "satellite_name", "confidence_class", "frp", "aoi"]].head(10))
