# Mawas Fire Early Warning

Streamlit dashboard showing NASA FIRMS VIIRS hotspots in the Mawas landscape
and surrounding villages, Central Kalimantan, Indonesia.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root with your FIRMS key
(get one at https://firms.modaps.eosdis.nasa.gov/api/map_key/):

```
FIRMS_MAP_KEY=your_key_here
```

## Usage

Quick check from the command line:

```powershell
python fetch_firms.py
```

Run the dashboard:

```powershell
streamlit run app.py
```

## AOIs

Every `.geojson` file in `aois/` is loaded as an AOI (any CRS; reprojected to
WGS84). `mawas.geojson` is the landscape; every other file is a village, named
from its `NAMOBJ` attribute (Indonesian village boundary data) or, failing
that, the file name. To add a village, drop its GeoJSON into `aois/`.

## How it works

- `fetch_firms.py` queries the FIRMS Area API in a bounding box covering
  Central Kalimantan (111–116°E, 4°S–0°) for these satellites:

  | Satellite | Sensor | Pixel | NRT source | Archive (SP) source |
  |---|---|---|---|---|
  | Suomi NPP | VIIRS | 375 m | `VIIRS_SNPP_NRT` | `VIIRS_SNPP_SP` (from 2012) |
  | NOAA-20 | VIIRS | 375 m | `VIIRS_NOAA20_NRT` | `VIIRS_NOAA20_SP` (from 2018) |
  | NOAA-21 | VIIRS | 375 m | `VIIRS_NOAA21_NRT` (from Jan 2024) | none |
  | Terra / Aqua | MODIS | 1 km | `MODIS_NRT` | `MODIS_SP` (from 2000) |

  - Recent dates use the NRT feeds; older dates use the standard-processed
    archive. The split point comes from the FIRMS data-availability endpoint,
    and dates before a satellite's first record are skipped.
  - Confidence is normalised to low / nominal / high (VIIRS reports `l/n/h`;
    MODIS reports 0–100 %, classed as <30 low, 30–79 nominal, ≥80 high), and
    only nominal and high are kept.
  - Hotspots inside any AOI are kept; each lists every AOI containing it
    (villages can overlap Mawas).
  - Ranges are fetched in 5-day requests (the API maximum).
- `app.py` lays out the dashboard:
  - **Map**: AOI boundaries (Mawas green, villages dashed blue, selected AOI
    black) and hotspots sized by fire radiative power (FRP), coloured by age
    measured back from the end of the selected period: red (last 24 hours,
    pulsing), orange (24–72 hours), yellow (older). OpenStreetMap or Esri
    satellite imagery basemap.
  - **Side panel**: hotspot counts for the last 24 hours and the whole period,
    the date range (last 24 hours / 3 / 7 / 30 days, or custom WIB dates up
    to 31 days) with a satellite selector, and the AOI dropdown, which filters hotspots and zooms the
    map, plus a per-AOI summary.
  - **Table**: every detection in the selected AOI and period with WIB date
    and time, confidence, coordinates, FRP, brightness, AOI, satellite and
    day/night. Filters for age, confidence, satellite, day/night and minimum
    FRP narrow the table and its Excel, CSV or KML downloads (`exports.py`).
  - FIRMS data is cached for 15 minutes.

Dates and times in the dashboard are WIB (UTC+7); the table also keeps the
original FIRMS acquisition time in UTC.
