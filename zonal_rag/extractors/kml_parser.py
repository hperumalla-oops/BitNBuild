"""Parse KML into GeoJSON FeatureCollections, streaming for large files.

Spatial outputs are unaffected by the OpenAI/Supabase migration — per
CLAUDE.md, `output/spatial/*.geojson` stays as flat files (only tabular
and RAG data moved into Postgres).
"""

import json
from pathlib import Path

from lxml import etree

KML_NS = "{http://www.opengis.net/kml/2.2}"


def kml_to_geojson(kml_path: Path, source_id: str) -> dict:
    """Streams the KML with iterparse so the ~70MB cadastral file never
    loads fully into memory."""
    features = []

    context = etree.iterparse(str(kml_path), events=("end",), tag=f"{KML_NS}Placemark")
    for _, placemark in context:
        feature = _placemark_to_feature(placemark, source_id)
        if feature is not None:
            features.append(feature)
        placemark.clear()
        while placemark.getprevious() is not None:
            del placemark.getparent()[0]

    return {"type": "FeatureCollection", "features": features}


def _placemark_to_feature(placemark, source_id: str) -> dict | None:
    geometry = _extract_geometry(placemark)
    if geometry is None:
        return None

    properties = {"source_id": source_id}
    for data in placemark.iter(f"{KML_NS}SimpleData"):
        name = data.get("name")
        if name:
            properties[name] = data.text

    name_el = placemark.find(f"{KML_NS}name")
    if name_el is not None and name_el.text:
        properties.setdefault("name", name_el.text)

    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _extract_geometry(placemark) -> dict | None:
    polygon = placemark.find(f".//{KML_NS}Polygon")
    if polygon is not None:
        coords = _polygon_coords(polygon)
        if coords:
            return {"type": "Polygon", "coordinates": coords}

    point = placemark.find(f".//{KML_NS}Point")
    if point is not None:
        coords_el = point.find(f"{KML_NS}coordinates")
        if coords_el is not None and coords_el.text:
            lon, lat, *_ = [float(v) for v in coords_el.text.strip().split(",")]
            return {"type": "Point", "coordinates": [lon, lat]}

    line = placemark.find(f".//{KML_NS}LineString")
    if line is not None:
        coords_el = line.find(f"{KML_NS}coordinates")
        if coords_el is not None and coords_el.text:
            return {"type": "LineString", "coordinates": _parse_coord_string(coords_el.text)}

    return None


def _polygon_coords(polygon) -> list:
    rings = []
    outer = polygon.find(f".//{KML_NS}outerBoundaryIs/{KML_NS}LinearRing/{KML_NS}coordinates")
    if outer is None or not outer.text:
        return []
    rings.append(_parse_coord_string(outer.text))

    for inner in polygon.findall(f".//{KML_NS}innerBoundaryIs/{KML_NS}LinearRing/{KML_NS}coordinates"):
        if inner.text:
            rings.append(_parse_coord_string(inner.text))

    return rings


def _parse_coord_string(text: str) -> list:
    coords = []
    for triplet in text.strip().split():
        parts = triplet.split(",")
        lon, lat = float(parts[0]), float(parts[1])
        coords.append([lon, lat])
    return coords


def write_geojson(geojson: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)
