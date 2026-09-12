"""
Tools the chat agent can call.

Two hard-scoped groups plus a shared one. The mode the user picks in the UI
decides which group the model is even shown, so it cannot answer a zoning
question out of the map tables or vice versa.

Everything goes through PostgREST with the service key. No tool ever builds
SQL from model output — table names and metric names are whitelisted here, and
values travel as parameters.
"""

import json
import os

import requests

TIMEOUT = 45
EMBEDDING_MODEL = "text-embedding-3-small"   # must match the 1536-dim vectors in rag_chunks

# Structured zoning tables the agent may read, with the columns worth showing.
ZONING_TABLES = {
    "permitted_uses":  "zone, use_type, is_permitted, conditions, source_id, source_page",
    "far_rules":       "zone, ring, road_width_m, far, notes, source_id, source_page",
    "setback_rules":   "zone, plot_size_sqm, front_m, rear_m, side1_m, side2_m, source_id, source_page",
    "height_rules":    "zone, ring, road_width_m, max_height_m, notes, source_id, source_page",
    "parking_rules":   "zone, use_type, requirement, unit, source_id, source_page",
    "coverage_rules":  "zone, plot_size_sqm, max_coverage_pct, source_id, source_page",
    "plot_size_rules": "zone, use_type, min_plot_sqm, min_frontage_m, source_id, source_page",
    "density_rules":   "zone, max_dwelling_units_per_hectare, notes, source_id, source_page",
}

RANK_METRICS = [
    "report_total", "report_unresolved", "report_density", "reports_per_1000",
    "civic_pressure_score", "population", "pop_density", "area_sq_km",
    "allowed_height_m",
]


class Tools:
    def __init__(self, supabase_url, service_key, openai_client, tavily_key=None):
        self.url = supabase_url.rstrip("/")
        self.h = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self.openai = openai_client
        self.tavily_key = tavily_key

    # -- plumbing ----------------------------------------------------------
    def _rpc(self, name, payload):
        r = requests.post(f"{self.url}/rest/v1/rpc/{name}", headers=self.h,
                          data=json.dumps(payload), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _select(self, table, params):
        r = requests.get(f"{self.url}/rest/v1/{table}", headers=self.h,
                         params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    # -- zoning ------------------------------------------------------------
    def zoning_search(self, query, match_count=8, match_threshold=0.3):
        """Semantic search over the regulation text."""
        emb = self.openai.embeddings.create(
            model=EMBEDDING_MODEL, input=query
        ).data[0].embedding
        rows = self._rpc("match_rag_chunks", {
            "query_embedding": emb,
            "match_threshold": match_threshold,
            "match_count": max(1, min(int(match_count), 20)),
        })
        # Attach a deep link to the exact PDF page so answers can cite a source.
        srcs = {s["source_id"]: s for s in self._select("sources", {"select": "*"})}
        for row in rows:
            s = srcs.get(row.get("source_id"), {})
            row["source_name"] = s.get("filename") or s.get("description")
            if s.get("url") and row.get("page_number"):
                row["pdf_url"] = f"{s['url']}#page={row['page_number']}"
        return {"matches": rows, "note":
                "Prose from the regulations. For exact numbers prefer zoning_rules."}

    def zoning_rules(self, table, zone=None, use_type=None, limit=40):
        """Exact numeric rules out of the structured tables."""
        if table not in ZONING_TABLES:
            return {"error": f"unknown table {table!r}",
                    "available": sorted(ZONING_TABLES)}
        params = {"select": "*", "limit": max(1, min(int(limit), 100))}
        if zone:
            params["zone"] = f"ilike.*{zone}*"
        if use_type:
            params["use_type"] = f"ilike.*{use_type}*"
        try:
            rows = self._select(table, params)
        except requests.HTTPError:
            # a column named in the filter may not exist on this table
            rows = self._select(table, {"select": "*", "limit": params["limit"]})
        return {"table": table, "rows": rows, "row_count": len(rows)}

    def zoning_sources(self):
        return {"sources": self._select("sources", {"select": "*"})}

    # -- map ---------------------------------------------------------------
    def ward_lookup(self, query, limit=8):
        rows = self._rpc("ward_search", {"p_query": query, "p_limit": int(limit)})
        for r in rows:
            r["map_link"] = f"?tab=map&ward={r['ward_key']}"
        return {"wards": rows, "match_count": len(rows)}

    def ward_rank(self, metric, limit=10, corporation=None, ascending=False):
        if metric not in RANK_METRICS:
            return {"error": f"unknown metric {metric!r}", "available": RANK_METRICS}
        rows = self._rpc("ward_rank", {
            "p_metric": metric, "p_limit": int(limit),
            "p_corporation": corporation, "p_ascending": bool(ascending),
        })
        for r in rows:
            r["map_link"] = f"?tab=map&ward={r['ward_key']}&metric={_ui_metric(metric)}"
        return {"metric": metric, "ascending": bool(ascending), "wards": rows}

    def ward_reports(self, ward_key):
        rows = self._rpc("ward_reports_for", {"p_ward_key": ward_key})
        return {"ward_key": ward_key, "reports": rows, "report_count": len(rows),
                "note": "Only 4 wards have itemised reports; the rest carry "
                        "headline counts only."}

    def ward_at_point(self, lat, lng):
        rows = self._rpc("ward_at_point", {"p_lat": float(lat), "p_lng": float(lng)})
        if not rows:
            return {"ward": None,
                    "note": "That point is outside the 369-ward GBA layer."}
        rows[0]["map_link"] = f"?tab=map&ward={rows[0]['ward_key']}"
        return {"ward": rows[0]}

    def stations_near(self, lat, lng, radius_m=2000):
        rows = self._rpc("stations_near", {
            "p_lat": float(lat), "p_lng": float(lng),
            "p_radius_m": float(radius_m)})
        for r in rows:
            r["map_link"] = f"?tab=map&station={r['id']}"
        return {"stations": rows, "station_count": len(rows)}

    def projects_in_ward(self, ward_key):
        rows = self._rpc("projects_in_ward", {"p_ward_key": ward_key})
        for r in rows:
            r["map_link"] = f"?tab=map&project={r['id']}"
        return {"ward_key": ward_key, "projects": rows}

    def station_lookup(self, query, limit=10):
        rows = self._select("stations", {
            "select": "id,name,status,service_date,interchange,ward_key,lat,lng",
            "name": f"ilike.*{query}*",
            "limit": max(1, min(int(limit), 30)),
        })
        for r in rows:
            r["map_link"] = f"?tab=map&station={r['id']}"
        return {"stations": rows}

    # -- shared ------------------------------------------------------------
    def web_search(self, query, max_results=5):
        if not self.tavily_key:
            return {"error": "TAVILY_API_KEY is not set; web search unavailable."}
        r = requests.post("https://api.tavily.com/search", timeout=TIMEOUT, json={
            "api_key": self.tavily_key,
            "query": query,
            "max_results": max(1, min(int(max_results), 10)),
            "search_depth": "basic",
            "include_answer": True,
        })
        if r.status_code >= 300:
            return {"error": f"Tavily returned {r.status_code}", "body": r.text[:400]}
        d = r.json()
        return {
            "answer": d.get("answer"),
            "results": [{"title": x.get("title"), "url": x.get("url"),
                         "content": (x.get("content") or "")[:1200]}
                        for x in d.get("results", [])],
        }


def _ui_metric(metric):
    """Map a database column to the metric name map.html's dropdown uses."""
    return {
        "report_total": "reportTotal", "report_unresolved": "reportUnresolved",
        "report_density": "reportDensity", "reports_per_1000": "reportsPer1000",
        "civic_pressure_score": "civicPressureScore", "population": "population",
        "pop_density": "popDensity", "area_sq_km": "areaSqKm",
        "allowed_height_m": "allowedHeightM",
    }.get(metric, "reportTotal")


# --------------------------------------------------------------------------- #
# Schemas handed to the model. Split by mode — "hard scope" means the model is
# never shown the other dataset's tools.
# --------------------------------------------------------------------------- #

def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": list(required)}}}


WEB_TOOLS = [
    _fn("web_search",
        "Search the live web. Use for anything outside the two databases: "
        "current news, recent rule changes, or context about a place.",
        {"query": {"type": "string"},
         "max_results": {"type": "integer", "description": "1-10, default 5"}},
        ["query"]),
]

ZONING_TOOLS = [
    _fn("zoning_search",
        "Semantic search over 2,629 chunks of Bengaluru zoning regulation text. "
        "Best for prose, definitions and clause wording. Returns a pdf_url "
        "deep-linked to the exact source page — always cite it.",
        {"query": {"type": "string"},
         "match_count": {"type": "integer", "description": "1-20, default 8"}},
        ["query"]),
    _fn("zoning_rules",
        "Exact numeric zoning rules from the structured tables. Prefer this "
        "over zoning_search whenever the question asks for a number (FAR, "
        "setback, height, parking, coverage, plot size, density) or whether a "
        "use is permitted.",
        {"table": {"type": "string", "enum": sorted(ZONING_TABLES)},
         "zone": {"type": "string", "description": "zone code, e.g. 'R1', 'MU3'"},
         "use_type": {"type": "string"},
         "limit": {"type": "integer", "description": "1-100, default 40"}},
        ["table"]),
    _fn("zoning_sources",
        "List the source documents behind the zoning data, with their URLs.",
        {}),
]

MAP_TOOLS = [
    _fn("ward_lookup",
        "Find wards by name, zone name or ward key ('5:25'). Returns civic, "
        "demographic and report figures plus a map_link.",
        {"query": {"type": "string"},
         "limit": {"type": "integer", "description": "1-50, default 8"}},
        ["query"]),
    _fn("ward_rank",
        "Rank wards by a metric — use for 'worst/most/least/top N' questions. "
        "Prefer report_density or reports_per_1000 over report_total: raw "
        "counts mostly track how big a ward is.",
        {"metric": {"type": "string", "enum": RANK_METRICS},
         "limit": {"type": "integer", "description": "1-50, default 10"},
         "corporation": {"type": "string",
                         "enum": ["Central", "East", "North", "South", "West"]},
         "ascending": {"type": "boolean", "description": "true for lowest-first"}},
        ["metric"]),
    _fn("ward_reports",
        "The itemised civic reports for one ward, by ward_key. Only 4 wards "
        "have these; every other ward has headline counts only.",
        {"ward_key": {"type": "string"}}, ["ward_key"]),
    _fn("ward_at_point",
        "Which ward contains a latitude/longitude.",
        {"lat": {"type": "number"}, "lng": {"type": "number"}}, ["lat", "lng"]),
    _fn("stations_near",
        "Metro stations within a radius of a point, nearest first.",
        {"lat": {"type": "number"}, "lng": {"type": "number"},
         "radius_m": {"type": "number", "description": "metres, default 2000"}},
        ["lat", "lng"]),
    _fn("station_lookup",
        "Find metro stations by name.",
        {"query": {"type": "string"},
         "limit": {"type": "integer"}}, ["query"]),
    _fn("projects_in_ward",
        "Road infrastructure projects whose geometry touches a ward.",
        {"ward_key": {"type": "string"}}, ["ward_key"]),
]


def schemas_for(mode):
    return (ZONING_TOOLS if mode == "zoning" else MAP_TOOLS) + WEB_TOOLS


def names_for(mode):
    return {t["function"]["name"] for t in schemas_for(mode)}
