#!/usr/bin/env python3
"""
build_map_data.py — joins every ward-level source into one GeoJSON and every
coordinate-level source into one points file, ready for map.html to fetch.

Outputs (next to this script):
  wards.geojson  — 369 GBA wards, all attributes pre-joined into properties
  points.json    — metro stations + road infra projects, with their news

Run:  python build_map_data.py
"""

import json
import math
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"        # source inputs, committed
OUT = ROOT / "output"       # everything generated
OUT.mkdir(exist_ok=True)

# source inputs
GBA_GEOJSON = DATA / "bengaluru-gba-wards-2025.geojson"
CIVIC = DATA / "civic-database.json"
BBMP198 = DATA / "bbmp_wards.json"
W243 = DATA / "bbmp_243_wards_mp_mla.json"
STATIONS = DATA / "stations.json"
INFRA = DATA / "infra_projects.json"

# produced by the parsers in this folder
THINGS = OUT / "things.json"
NEWS = OUT / "news.json"
ROAD_NEWS = OUT / "road_news.json"

# what web/map.html reads
OUT_WARDS = OUT / "wards.geojson"
OUT_POINTS = OUT / "points.json"

COORD_PRECISION = 5


def load(path, default=None):
    if not path.exists():
        return default
    # Some inputs carry stray non-UTF8 bytes; replace rather than crash.
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def norm_name(s):
    """Loose ward-name key for the two legacy sources that have no ward id."""
    s = (s or "").lower().replace("ward", "").replace("nagara", "nagar")
    s = s.replace("halli", "alli")
    return re.sub(r"[^a-z0-9]", "", s)


# --------------------------------------------------------------------------- #
# MLA / MP photographs
# --------------------------------------------------------------------------- #

PHOTO_DIR = DATA / "mla_photos"
WEB = ROOT / "web"

# The two lists spell one MLA differently — the same person, Hebbal's sitting
# member. Without this he is the only name in the data with no photo.
NAME_ALIASES = {
    "byrathi suresh": "B. S. Suresha",
}


def person_key(name):
    """Match a person across sources: drop honorifics, punctuation and case."""
    s = (name or "").lower()
    s = NAME_ALIASES.get(s.strip(), s)
    if not isinstance(s, str):
        s = name
    s = s.lower()
    s = re.sub(r"\b(dr|shri|smt|sri|mr|ms)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def photo_index():
    """{person_key: filename} for every photo on disk."""
    if not PHOTO_DIR.exists():
        return {}
    return {person_key(p.stem.replace("-", " ")): p.name
            for p in sorted(PHOTO_DIR.glob("*.png"))}


# --------------------------------------------------------------------------- #
# Geometry normalisation
# --------------------------------------------------------------------------- #

def swap_ring(ring):
    """The GBA file stores [lat, lng]; GeoJSON and Leaflet want [lng, lat]."""
    return [[round(p[1], COORD_PRECISION), round(p[0], COORD_PRECISION)] for p in ring]


def normalise_geometry(geom):
    """Return a Polygon/MultiPolygon with [lng, lat] coords.

    19 features arrive as a GeometryCollection holding one Polygon plus a
    stray LineString (a KML export artifact) — keep the polygons, drop the rest.
    """
    t = geom["type"]
    if t == "Polygon":
        return {"type": "Polygon", "coordinates": [swap_ring(r) for r in geom["coordinates"]]}
    if t == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[swap_ring(r) for r in poly] for poly in geom["coordinates"]]}
    if t == "GeometryCollection":
        parts = [normalise_geometry(g) for g in geom["geometries"]
                 if g["type"] in ("Polygon", "MultiPolygon")]
        polys = []
        for p in parts:
            polys += [p["coordinates"]] if p["type"] == "Polygon" else p["coordinates"]
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": polys[0]}
        return {"type": "MultiPolygon", "coordinates": polys}
    raise ValueError(f"unexpected geometry type {t}")


def polygons_of(geom):
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def ring_area_km2(ring, lat0):
    """Shoelace on a local equirectangular projection — accurate enough for
    ward-sized areas and avoids a pyproj dependency."""
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0] * kx, ring[i][1] * ky
        x2, y2 = ring[i + 1][0] * kx, ring[i + 1][1] * ky
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def geometry_metrics(geom):
    """Area in sq km (outer rings minus holes) and a centroid for labelling."""
    lats = [p[1] for poly in polygons_of(geom) for ring in poly for p in ring]
    lat0 = sum(lats) / len(lats)
    area = 0.0
    best_ring, best_area = None, -1.0
    for poly in polygons_of(geom):
        outer = ring_area_km2(poly[0], lat0)
        if outer > best_area:
            best_area, best_ring = outer, poly[0]
        area += outer - sum(ring_area_km2(h, lat0) for h in poly[1:])
    cx = sum(p[0] for p in best_ring) / len(best_ring)
    cy = sum(p[1] for p in best_ring) / len(best_ring)
    return round(area, 3), [round(cx, COORD_PRECISION), round(cy, COORD_PRECISION)]


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


def locate_ward(lng, lat, features):
    for f in features:
        for poly in polygons_of(f["geometry"]):
            if point_in_ring(lng, lat, poly[0]) and not any(
                point_in_ring(lng, lat, h) for h in poly[1:]
            ):
                return f["properties"]["wardKey"]
    return None


# --------------------------------------------------------------------------- #
# Build wards
# --------------------------------------------------------------------------- #

def build_wards():
    gba = load(GBA_GEOJSON)
    civic = {(w["corporationId"], w["wardId"]): w for w in load(CIVIC)["wards"]}
    things_raw = load(THINGS)["sections"]

    corp_id = {w["corporation"]: w["corporationId"] for w in load(CIVIC)["wards"]}
    things = {(corp_id[s["zone"]], s["ward_number"]): s for s in things_raw}

    # Legacy sources have no ward id, so they join on name and only cover part
    # of the city. They contribute fields nothing else has.
    mla = {norm_name(w["ward_name"]): w for w in load(W243, [])}
    old = {norm_name(f["properties"]["WARD_NAME"]): f["properties"]
           for f in load(BBMP198, {"features": []})["features"]}

    photos = photo_index()

    features, stats = [], Counter()
    for f in gba["features"]:
        p = f["properties"]
        cid, wid = int(p["corporation_id"]), int(p["ward_id"])
        key = f"{cid}:{wid}"
        geom = normalise_geometry(f["geometry"])
        area, centroid = geometry_metrics(geom)

        c = civic.get((cid, wid), {})
        t = things.get((cid, wid))
        nm = norm_name(p["ward_name"])
        m = mla.get(nm, {})
        o = old.get(nm, {})

        pop = c.get("population") or int(float(p["TOT_P"] or 0))
        total = t["total"] if t else None
        unresolved = t["unresolved"] if t else None

        props = {
            "wardKey": key,
            "wardId": wid,
            "wardName": p["ward_name"],
            "wardNameKn": p["ward_name_kn"],
            "corporation": p["Corporation"],
            "corporationId": cid,
            "zoneName": p["zone_name"],
            "assembly": p["Assembly"],
            "roDivision": p["RO_Division"],
            "aroSubDivision": p.get("ARO_ Sub Division"),
            # demographics
            "population": pop,
            "popMale": int(float(p["TOT_M"] or 0)),
            "popFemale": int(float(p["TOT_F"] or 0)),
            "popSC": int(float(p["SC_P"] or 0)),
            "popST": int(float(p["ST_P"] or 0)),
            "areaSqKm": area,
            "popDensity": round(pop / area, 1) if area else None,
            "centroid": centroid,
            # civic attributes
            "propertyType": c.get("propertyType"),
            "zoningClass": c.get("zoningClass"),
            "allowedHeightM": c.get("allowedHeightM"),
            "currentAverageHeightM": c.get("currentAverageHeightM"),
            "requestedScenarioHeightM": c.get("requestedScenarioHeightM"),
            "heightBand": c.get("heightBand"),
            "halClearanceStatus": c.get("halClearanceStatus"),
            "civicPressureScore": c.get("civicPressureScore"),
            "campaignReadiness": c.get("campaignReadiness"),
            "primaryConcerns": c.get("primaryConcerns", []),
            "recommendedNextAction": c.get("recommendedNextAction"),
            "dumpRecordsLinked": c.get("dumpRecordsLinked"),
            # reports (null where the ward has no entry at all)
            "hasReports": t is not None,
            "reportTotal": total,
            "reportUnresolved": unresolved,
            "reportDensity": round(total / area, 1) if (total and area) else None,
            "reportsPer1000": round(total / pop * 1000, 2) if (total and pop) else None,
            "statusCounts": t.get("status_counts_in_html") if t else {},
            "reports": t.get("reports", []) if t else [],
            "representative": t.get("representative") if t else None,
            # things.json's "representative" IS the sitting MLA: it agrees
            # with the 243-ward list on 114 of the 118 wards carrying both, and
            # the 4 exceptions are spelling variants of one person. It covers
            # 350 wards against that list's 123, so the UI prefers it. Both are
            # emitted unchanged so this file matches the shape wards_geojson()
            # returns from Supabase.
            "mpName": m.get("MP_name"),
            "mlaName": m.get("MLA_name"),
            "reservation": o.get("RESERVATIO"),
            "legacyWardNo": o.get("WARD_NO"),
        }

        stats["with_mla_photo"] += 1 if photos.get(
            person_key(props["representative"] or props["mlaName"])) else 0
        stats["with_mp_photo"] += 1 if photos.get(person_key(props["mpName"])) else 0
        stats["with_reports"] += 1 if t else 0
        stats["with_detail"] += 1 if (t and t.get("reports")) else 0
        stats["with_mla"] += 1 if m else 0
        stats["with_legacy"] += 1 if o else 0
        features.append({"type": "Feature", "properties": props, "geometry": geom})

    return {"type": "FeatureCollection", "features": features}, stats


# --------------------------------------------------------------------------- #
# Build points
# --------------------------------------------------------------------------- #

def haversine_km(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def line_sequences(raw_stations):
    """Running order of each metro line, for drawing it as a connected route.

    stations.json lists each line's stations in order, but an interchange is
    listed once under its *first* line — so filtering by line leaves it in the
    wrong slot (Pink jumped 6.6 km at MG Road, Blue 13.8 km at Nagawara).
    Stations whose first line is this one are already correct; the rest are
    inserted where they add the least path length.
    """
    lines = {}
    for name in {ln for s in raw_stations for ln in s["lines"]}:
        mine = [s for s in raw_stations if name in s["lines"]]
        seq = [s for s in mine if s["lines"][0] == name]
        for s in [s for s in mine if s["lines"][0] != name]:
            pt = (s["lat"], s["lng"])
            best, at = None, 0
            for i in range(len(seq) + 1):
                if i == 0:
                    cost = haversine_km(pt, (seq[0]["lat"], seq[0]["lng"]))
                elif i == len(seq):
                    cost = haversine_km((seq[-1]["lat"], seq[-1]["lng"]), pt)
                else:
                    a = (seq[i - 1]["lat"], seq[i - 1]["lng"])
                    b = (seq[i]["lat"], seq[i]["lng"])
                    cost = haversine_km(a, pt) + haversine_km(pt, b) - haversine_km(a, b)
                if best is None or cost < best:
                    best, at = cost, i
            seq.insert(at, s)
        out, seen = [], set()
        for s in seq:
            if s["id"] not in seen:
                seen.add(s["id"])
                out.append(s["id"])
        lines[name] = out
    return lines


def build_points(wards):
    stations_doc = load(STATIONS)
    news = load(NEWS, {})
    infra_doc = load(INFRA)
    road_news = load(ROAD_NEWS, {})

    # A few interchanges are listed once per line in stations.json — same id,
    # same coordinates, differing only in `lines`. Merge them into one station
    # carrying every line, which is what the id being a primary key implies.
    stations = []
    by_id = {}
    for s in stations_doc["stations"]:
        rec = by_id.get(s["id"])
        if rec is None:
            rec = {
                "id": s["id"],
                "name": s["name"],
                "lat": s["lat"],
                "lng": s["lng"],
                "lines": list(s["lines"]),
                "status": s["status"],
                "date": s.get("date"),
                "interchange": s.get("interchange", False),
                "note": s.get("note", ""),
                "wardKey": locate_ward(s["lng"], s["lat"], wards["features"]),
                "news": news.get(s["id"], []),
            }
            by_id[s["id"]] = rec
            stations.append(rec)
            continue
        for line in s["lines"]:
            if line not in rec["lines"]:
                rec["lines"].append(line)
        if not rec["note"] and s.get("note"):
            rec["note"] = s["note"]
    # serving more than one line is what an interchange is
    for rec in stations:
        if len(rec["lines"]) > 1:
            rec["interchange"] = True

    projects = []
    for p in infra_doc["projects"]:
        anchor = p["point"] or p["path"][0]  # infra coords are [lat, lng]
        projects.append({
            "id": p["id"],
            "name": p["name"],
            "type": p["type"],
            "authority": p["authority"],
            "status": p["status"],
            "lengthKm": p.get("length_km"),
            "costCrore": p.get("cost_crore"),
            "expectedCompletion": p.get("expected_completion"),
            "description": p.get("description", ""),
            "point": p["point"],
            "path": p["path"],
            "wardKey": locate_ward(anchor[1], anchor[0], wards["features"]),
            "news": road_news.get(p["id"], []),
        })

    # jsonb normalises object key order, so the source order of the lines
    # would be lost on a Supabase round-trip. Carry it explicitly, along with
    # each line's running order so the map can draw it as a route.
    seqs = line_sequences(stations_doc["stations"])
    lines = {name: dict(spec, order=i + 1, stations=seqs.get(name, []))
             for i, (name, spec) in enumerate(stations_doc["lines"].items())}

    return {
        "metroLines": lines,
        "stations": stations,
        "infraLegend": infra_doc.get("legend", {}),
        "projects": projects,
        "lineNews": {k: v for k, v in news.items() if k == "_lines"}.get("_lines", {}),
        "roadNewsGeneral": road_news.get("_general", []),
        "roadNewsAuthorities": road_news.get("_authorities", {}),
    }


def write_photo_manifest(wards):
    """Exact-name -> photo filename, for every person named in the data.

    Keyed by the literal string the UI displays so the browser does a dictionary
    lookup, with no second copy of the name-matching rules to drift out of sync.
    """
    photos = photo_index()
    manifest = {}
    for f in wards["features"]:
        for field in ("representative", "mlaName", "mpName"):
            name = f["properties"].get(field)
            if not name:
                continue
            hit = photos.get(person_key(name))
            if hit:
                manifest[name] = hit
    WEB.mkdir(exist_ok=True)
    (WEB / "mla-photos.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8")
    unmatched = sorted({f["properties"][k] for f in wards["features"]
                        for k in ("representative", "mlaName", "mpName")
                        if f["properties"].get(k) and f["properties"][k] not in manifest})
    return len(manifest), unmatched


def main():
    wards, stats = build_wards()
    OUT_WARDS.write_text(json.dumps(wards, separators=(",", ":"), ensure_ascii=False),
                         encoding="utf-8")

    points = build_points(wards)
    OUT_POINTS.write_text(json.dumps(points, separators=(",", ":"), ensure_ascii=False),
                          encoding="utf-8")

    n = len(wards["features"])
    print(f"wards.geojson  {n} wards, {OUT_WARDS.stat().st_size/1e6:.2f} MB")
    print(f"  with report counts : {stats['with_reports']}")
    print(f"  with itemised reports: {stats['with_detail']}")
    print(f"  with MP/MLA        : {stats['with_mla']}")
    print(f"  with legacy census : {stats['with_legacy']}")
    n_photos, unmatched = write_photo_manifest(wards)
    print(f"  with an MLA photo  : {stats['with_mla_photo']}")
    print(f"  with an MP photo   : {stats['with_mp_photo']}")
    print(f"mla-photos.json  {n_photos} names mapped"
          + (f", UNMATCHED: {unmatched}" if unmatched else ", none unmatched"))
    located = sum(1 for s in points["stations"] if s["wardKey"])
    withnews = sum(1 for s in points["stations"] if s["news"])
    print(f"points.json    {len(points['stations'])} stations "
          f"({located} inside a ward, {withnews} with news), "
          f"{len(points['projects'])} projects, "
          f"{OUT_POINTS.stat().st_size/1e6:.2f} MB")


if __name__ == "__main__":
    main()
