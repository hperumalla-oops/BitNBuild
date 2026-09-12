#!/usr/bin/env python3
"""
load_supabase.py — push output/wards.geojson and output/points.json into
Supabase.

Run migrations/002_ward_atlas.sql first; this calls the import_wards / import_points
functions that file creates. Both are full reloads and safe to re-run.

Credentials come from the repo-root .env (SUPABASE_URL, SUPABASE_SECRET_KEY) —
the service key, because the import functions are deliberately not granted
to anon.

Usage:
    python parsers/load_supabase.py
    python parsers/load_supabase.py --verify-only
"""

import argparse
import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent          # BitNBuild/
REPO = ROOT.parent          # repo root, where the shared .env lives
OUT = ROOT / "output"
ENV_FILE = REPO / ".env"

WARDS = OUT / "wards.geojson"
POINTS = OUT / "points.json"

TIMEOUT = 180


def load_env(path):
    """Minimal .env reader — avoids a python-dotenv dependency."""
    if not path.exists():
        sys.exit(f"No .env at {path}")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    ap = argparse.ArgumentParser(description="Load the map data into Supabase")
    ap.add_argument("--verify-only", action="store_true",
                    help="Skip the import, just report row counts")
    args = ap.parse_args()

    env = load_env(ENV_FILE)
    url = env.get("SUPABASE_URL", "").rstrip("/")
    key = env.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SECRET_KEY must both be set in .env")

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    def rpc(name, payload):
        r = requests.post(f"{url}/rest/v1/rpc/{name}", headers=headers,
                          data=json.dumps(payload), timeout=TIMEOUT)
        if r.status_code >= 300:
            sys.exit(f"\n{name} failed [{r.status_code}]\n{r.text[:1500]}")
        return r.text.strip()

    def count(table):
        r = requests.get(f"{url}/rest/v1/{table}",
                         headers={**headers, "Prefer": "count=exact",
                                  "Range-Unit": "items", "Range": "0-0"},
                         params={"select": "*"}, timeout=TIMEOUT)
        if r.status_code >= 300:
            return f"error {r.status_code}"
        # Content-Range looks like "0-0/369"
        cr = r.headers.get("Content-Range", "")
        return cr.split("/")[-1] if "/" in cr else "?"

    def report(label, name, payload, path):
        print(f"{label}  {path.stat().st_size / 1e6:.2f} MB -> {name} …",
              end="", flush=True)
        out = rpc(name, payload)
        print(" ok")
        try:
            data = json.loads(out)
        except ValueError:
            print(f"    {out}")
            return
        for k, v in data.items():
            # anything skipped is a row the import refused; it should be 0
            flag = "  <-- SKIPPED" if k.endswith("_skipped") and v else ""
            print(f"    {k:<22} {v}{flag}")

    if not args.verify_only:
        for f in (WARDS, POINTS):
            if not f.exists():
                sys.exit(f"{f} missing — run build_map_data.py first")

        report("wards ", "import_wards",
               {"fc": json.loads(WARDS.read_text(encoding="utf-8"))}, WARDS)
        report("points", "import_points",
               {"doc": json.loads(POINTS.read_text(encoding="utf-8"))}, POINTS)

    print("\nrow counts")
    for t in ("wards", "ward_status_counts", "ward_reports", "metro_lines",
              "stations", "station_lines", "infra_projects",
              "news_items", "news_links"):
        print(f"  {t:<20} {count(t):>6}")


if __name__ == "__main__":
    main()
