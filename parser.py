#!/usr/bin/env python3
"""
parser.py — Namma Metro news/status parser

Runs forever (or once with --once). Every cycle it:
  1. Pulls Google News RSS for each metro line ("Namma Metro Purple Line", etc.)
     and for a set of high-signal keywords (land acquisition, BMRCL, opening date).
  2. Pulls Google News RSS for every individual station name (batched, politely
     rate-limited) so station-level timelines stay current.
  3. Fetches BMRCL's own press-release / tender pages for land acquisition and
     project-status notices.
  4. De-dupes against what it already has, tags each item with the station(s)
     and/or line(s) it's relevant to, and writes everything to news.json next
     to this script — the same folder metro_map.html lives in, so the map picks
     it up automatically when both are served together.

Usage:
    pip install requests feedparser beautifulsoup4 --break-system-packages
    python3 parser.py            # runs continuously, one pass every INTERVAL_MINUTES
    python3 parser.py --once     # single pass, then exit (good for cron)
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
STATIONS_FILE = HERE / "stations.json"
NEWS_FILE = HERE / "news.json"
SEEN_FILE = HERE / ".parser_seen.json"
LOG_FILE = HERE / "parser.log"

INTERVAL_MINUTES = 180          # how often to run a full pass when looping
MAX_ITEMS_PER_STATION = 6
MAX_ITEMS_PER_LINE = 12
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; NammaMetroTracker/1.0; +https://example.local)"

LINE_KEYWORDS = {
    "Purple": ["Namma Metro Purple Line"],
    "Green": ["Namma Metro Green Line"],
    "Yellow": ["Namma Metro Yellow Line Electronic City"],
    "Pink": ["Namma Metro Pink Line", "Namma Metro Pink Line Nagawara"],
    "Blue": ["Namma Metro Blue Line airport", "Namma Metro airport metro KIAL"],
}

GENERAL_KEYWORDS = [
    "BMRCL land acquisition",
    "BMRCL metro news",
    "Namma Metro opening date",
    "Namma Metro Phase 3",
    "Bengaluru metro construction update",
]

BMRCL_PAGES = [
    "https://english.bmrc.co.in/press-release/",
    "https://bmrc.co.in/",
]

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# Use UTF-8 for both the log file and console output so titles like ₹
# do not crash the parser on Windows terminals configured for cp1252.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("metro-parser")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not parse %s (%s), starting fresh", path, e)
    return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def item_hash(title, link):
    return hashlib.sha256(f"{title}|{link}".encode("utf-8")).hexdigest()[:16]


def fetch_rss(query, limit=10):
    url = GOOGLE_NEWS_RSS.format(q=quote_plus(query))
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        out = []
        for entry in feed.entries[:limit]:
            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
            source = ""
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                source = entry.source.title
            out.append({
                "title": re.sub(r"\s+", " ", entry.title).strip(),
                "url": entry.link,
                "date": published,
                "source": source,
            })
        return out
    except Exception as e:
        log.warning("RSS fetch failed for %r: %s", query, e)
        return []


def fetch_bmrcl_notices():
    """Best-effort scrape of BMRCL's own site for press releases / tenders
    mentioning land acquisition or project status. BMRCL's site structure
    changes often, so this is deliberately defensive."""
    notices = []
    for url in BMRCL_PAGES:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                text = re.sub(r"\s+", " ", a.get_text()).strip()
                if not text or len(text) < 15:
                    continue
                if re.search(r"land acquisition|compensation|survey|notification|tender|rehabilitat", text, re.I):
                    href = a["href"]
                    if href.startswith("/"):
                        href = url.rstrip("/") + href
                    notices.append({
                        "title": text,
                        "url": href,
                        "date": None,
                        "source": "BMRCL",
                    })
        except Exception as e:
            log.info("BMRCL page %s not reachable right now (%s) — skipping", url, e)
    return notices


def build_station_index(stations_data):
    """Map a lowercase search-friendly station name -> station id."""
    idx = {}
    for s in stations_data["stations"]:
        idx[s["name"].lower()] = s["id"]
        # also index a shortened form (drop punctuation) to catch loose matches
        short = re.sub(r"[^a-z0-9 ]", "", s["name"].lower())
        idx[short] = s["id"]
    return idx


def tag_stations(title, station_index):
    title_l = re.sub(r"[^a-z0-9 ]", "", title.lower())
    hits = set()
    for name, sid in station_index.items():
        if len(name) < 4:
            continue
        if name in title_l:
            hits.add(sid)
    return list(hits)


# --------------------------------------------------------------------------- #
# Main pass
# --------------------------------------------------------------------------- #

def run_pass():
    if not STATIONS_FILE.exists():
        log.error("stations.json not found next to parser.py — put it in the same folder.")
        return

    stations_data = json.loads(STATIONS_FILE.read_text())
    station_index = build_station_index(stations_data)
    station_names = sorted({s["name"] for s in stations_data["stations"]})

    seen = load_json(SEEN_FILE, {})
    news_by_station = load_json(NEWS_FILE, {})
    line_updates = news_by_station.get("_lines", {})

    new_count = 0

    # 1. Per-line searches
    for line, queries in LINE_KEYWORDS.items():
        bucket = line_updates.get(line, [])
        for q in queries:
            for item in fetch_rss(q, limit=MAX_ITEMS_PER_LINE):
                h = item_hash(item["title"], item["url"])
                if h in seen:
                    continue
                seen[h] = True
                new_count += 1
                bucket.insert(0, item)
                # also tag any station named in the headline
                for sid in tag_stations(item["title"], station_index):
                    news_by_station.setdefault(sid, [])
                    if item not in news_by_station[sid]:
                        news_by_station[sid].insert(0, item)
                        news_by_station[sid] = news_by_station[sid][:MAX_ITEMS_PER_STATION]
        bucket = bucket[:MAX_ITEMS_PER_LINE]
        line_updates[line] = bucket
        time.sleep(1)  # be polite

    # 2. General / land-acquisition keywords -> goes into a shared "_general" bucket
    general_bucket = line_updates.get("_general", [])
    for q in GENERAL_KEYWORDS:
        for item in fetch_rss(q, limit=8):
            h = item_hash(item["title"], item["url"])
            if h in seen:
                continue
            seen[h] = True
            new_count += 1
            general_bucket.insert(0, item)
            for sid in tag_stations(item["title"], station_index):
                news_by_station.setdefault(sid, [])
                if item not in news_by_station[sid]:
                    news_by_station[sid].insert(0, item)
                    news_by_station[sid] = news_by_station[sid][:MAX_ITEMS_PER_STATION]
        time.sleep(1)
    line_updates["_general"] = general_bucket[:20]

    # 3. Per-station searches — only for under-construction / interchange stations
    #    by default, to keep request volume sane. Pass --all-stations to widen.
    priority_stations = [
        s for s in stations_data["stations"]
        if s["status"] != "operational"
    ]
    for s in priority_stations:
        query = f'"{s["name"]}" metro Bengaluru'
        for item in fetch_rss(query, limit=4):
            h = item_hash(item["title"], item["url"])
            if h in seen:
                continue
            seen[h] = True
            new_count += 1
            news_by_station.setdefault(s["id"], [])
            news_by_station[s["id"]].insert(0, item)
            news_by_station[s["id"]] = news_by_station[s["id"]][:MAX_ITEMS_PER_STATION]
        time.sleep(1)

    # 4. BMRCL site notices (land acquisition etc.) — tag by station/line mention
    for item in fetch_bmrcl_notices():
        h = item_hash(item["title"], item["url"])
        if h in seen:
            continue
        seen[h] = True
        new_count += 1
        general_bucket.insert(0, item)
        for sid in tag_stations(item["title"], station_index):
            news_by_station.setdefault(sid, [])
            news_by_station[sid].insert(0, item)
    line_updates["_general"] = general_bucket[:20]

    news_by_station["_lines"] = line_updates
    news_by_station["_meta"] = {
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "station_count": len(station_names),
    }

    save_json(NEWS_FILE, news_by_station)
    save_json(SEEN_FILE, seen)
    log.info("Pass complete: %d new items. news.json updated.", new_count)


def main():
    ap = argparse.ArgumentParser(description="Namma Metro news/status parser")
    ap.add_argument("--once", action="store_true", help="Run a single pass and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL_MINUTES,
                     help="Minutes between passes when looping (default: %(default)s)")
    args = ap.parse_args()

    if args.once:
        run_pass()
        return

    log.info("Starting continuous parser loop — every %d minutes. Ctrl+C to stop.", args.interval)
    while True:
        try:
            run_pass()
        except Exception:
            log.exception("Pass failed, will retry next cycle")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
