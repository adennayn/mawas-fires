"""Download builders for the hotspot table: Excel, CSV and KML."""

import io
from xml.sax.saxutils import escape

import pandas as pd

# KML colours are aabbggrr; keyed by the dashboard's age classes
KML_COLORS = {
    "Last 24 hours": "ff1c1ae3",  # red
    "24–72 hours": "ff007fff",  # orange
    "Older than 72 hours": "ff00d4ff",  # yellow
}
KML_ICON = "http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png"


def to_csv_bytes(table: pd.DataFrame) -> bytes:
    # utf-8-sig so Excel opens the en dash in age labels correctly
    return table.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(table: pd.DataFrame, info: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        table.to_excel(writer, sheet_name="Hotspots", index=False)
        pd.DataFrame(list(info.items()), columns=["Field", "Value"]).to_excel(
            writer, sheet_name="Info", index=False
        )
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max(len(str(c.value or "")) for c in col)
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 40)
    return buf.getvalue()


def to_kml_bytes(table: pd.DataFrame, title: str) -> bytes:
    """One Placemark per hotspot, coloured by age class, with all table columns as ExtendedData."""
    styles = "".join(
        f'<Style id="age{i}"><IconStyle><color>{color}</color><scale>0.8</scale>'
        f"<Icon><href>{KML_ICON}</href></Icon></IconStyle></Style>"
        for i, color in enumerate(KML_COLORS.values())
    )
    style_ids = {age: f"age{i}" for i, age in enumerate(KML_COLORS)}

    placemarks = []
    for _, row in table.iterrows():
        data = "".join(
            f'<Data name="{escape(str(col))}"><value>{escape(str(val))}</value></Data>'
            for col, val in row.items()
        )
        placemarks.append(
            "<Placemark>"
            f"<name>{escape(row['Date (WIB)'])} {escape(row['Time (WIB)'])} WIB</name>"
            f"<styleUrl>#{style_ids.get(row['Age'], 'age2')}</styleUrl>"
            f"<ExtendedData>{data}</ExtendedData>"
            f"<Point><coordinates>{row['Longitude (x)']},{row['Latitude (y)']},0</coordinates></Point>"
            "</Placemark>"
        )

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(title)}</name>{styles}{''.join(placemarks)}"
        "</Document></kml>"
    )
    return kml.encode("utf-8")
