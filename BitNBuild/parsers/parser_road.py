#!/usr/bin/env python3
"""
parser_road.py — Bengaluru road-infra (flyover/underpass/elevated corridor/
tunnel road) news parser.

Sibling script to parser.py, but tracks infra_projects.json instead of
stations.json. Runs forever (or once with --once). Every cycle it:

  1. Pulls Google News RSS for each project by name, plus a per-authority
     query (BBMP, GBA, BDA, BSMILE, NHAI) to catch coverage that doesn't
     name the project exactly.
  2. Pulls a set of general keyword searches (land acquisition, tender,
     deadline, DPR, elevated corridor) so new/renamed projects still surface
     even if they're not yet in infra_projects.json.
  3. Best-effort scrapes BBMP/GBA/BDA public notice pages for land
     acquisition / tender notices.
  4. De-dupes against what it has already seen, tags each item with the
     project id(s) it mentions, and writes everything to ../output/road_news.json,
     which build_map_data.py folds into points.json for web/map.html.

Usage:
    pip install requests feedparser beautifulsoup4 --break-system-packages
    python3 parser_road.py            # loop forever, one pass every INTERVAL_MINUTES
    python3 parser_road.py --once     # single pass then exit (good for cron)
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
ROOT = HERE.parent
DATA = ROOT / "data"        # source inputs, committed
OUT = ROOT / "output"       # everything generated
OUT.mkdir(exist_ok=True)
PROJECTS_FILE = DATA / "infra_projects.json"
NEWS_FILE = OUT / "road_news.json"
SEEN_FILE = OUT / ".parser_road_seen.json"
LOG_FILE = OUT / "parser_road.log"

INTERVAL_MINUTES = 180
MAX_ITEMS_PER_PROJECT = 6
MAX_ITEMS_PER_AUTHORITY = 10
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; BLRRoadInfraTracker/1.0; +https://example.local)"

AUTHORITY_QUERIES = {
    "BBMP": ["BBMP flyover underpass Bengaluru"],
    "GBA": ["Greater Bengaluru Authority flyover underpass"],
    "BDA": ["Bangalore Development Authority flyover underpass"],
    "BSMILE": ["Bengaluru Smart Infrastructure Limited elevated corridor"],
    "NHAI": ["NHAI Bengaluru underpass flyover"],
    "KRDCL": ["KRDCL Bengaluru road project"],
}

GENERAL_KEYWORDS = [
    "Bengaluru flyover land acquisition",
    "Bengaluru underpass tender",
    "Bengaluru elevated corridor DPR",
    "Bengaluru tunnel road project",
    "Bengaluru grade separator delay",
    "Peripheral Ring Road Bengaluru",
]

# Best-effort public notice pages. These change often; failures are logged
# and skipped rather than crashing the run.
AUTHORITY_PAGES = [
    "https://bbmp.gov.in/en/notifications",
    "https://bdabangalore.org/notifications.html",
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
log = logging.getLogger("road-parser")


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


def fetch_authority_notices():
    """Best-effort scrape of BBMP/BDA public notice pages for land
    acquisition, tender, or project-status notices. Site structures change
    often — this is deliberately defensive and never raises."""
    notices = []
    for url in AUTHORITY_PAGES:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                text = re.sub(r"\s+", " ", a.get_text()).strip()
                if not text or len(text) < 15:
                    continue
                if re.search(
                    r"flyover|underpass|elevated corridor|grade separator|tunnel road|"
                    r"land acquisition|compensation|tender|rehabilitat",
                    text, re.I,
                ):
                    href = a["href"]
                    if href.startswith("/"):
                        href = url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + href
                    notices.append({
                        "title": text,
                        "url": href,
                        "date": None,
                        "source": url.split("/")[2],
                    })
        except Exception as e:
            log.info("Authority page %s not reachable right now (%s) — skipping", url, e)
    return notices


def build_project_index(projects_data):
    """Map lowercase name fragments -> project id, so a headline that names
    a junction or corridor can be tagged even without an exact title match."""
    idx = {}
    for p in projects_data["projects"]:
        idx[p["name"].lower()] = p["id"]
        short = re.sub(r"[^a-z0-9 ]", "", p["name"].lower())
        idx[short] = p["id"]
        # also index the bit before the first '(' or '–' as a looser key
        loose = re.split(r"[(–-]", p["name"].lower())[0].strip()
        if len(loose) > 6:
            idx.setdefault(loose, p["id"])
    return idx


def tag_projects(title, project_index):
    title_l = re.sub(r"[^a-z0-9 ]", "", title.lower())
    hits = set()
    for name, pid in project_index.items():
        if len(name) < 6:
            continue
        if name in title_l:
            hits.add(pid)
    return list(hits)


# --------------------------------------------------------------------------- #
# Main pass
# --------------------------------------------------------------------------- #

def run_pass():
    if not PROJECTS_FILE.exists():
        log.error("infra_projects.json not found at %s — it belongs in the data/ folder.", PROJECTS_FILE)
        return

    projects_data = json.loads(PROJECTS_FILE.read_text())
    project_index = build_project_index(projects_data)

    seen = load_json(SEEN_FILE, {})
    news_by_project = load_json(NEWS_FILE, {})
    authority_updates = news_by_project.get("_authorities", {})
    general_bucket = news_by_project.get("_general", [])

    new_count = 0

    def file_item(item, bucket_list, project_hint_pids=None):
        nonlocal new_count
        h = item_hash(item["title"], item["url"])
        if h in seen:
            return
        seen[h] = True
        new_count += 1
        bucket_list.insert(0, item)
        pids = set(project_hint_pids or [])
        pids.update(tag_projects(item["title"], project_index))
        for pid in pids:
            news_by_project.setdefault(pid, [])
            if item not in news_by_project[pid]:
                news_by_project[pid].insert(0, item)
                news_by_project[pid] = news_by_project[pid][:MAX_ITEMS_PER_PROJECT]

    # 1. Per-project name search
    for p in projects_data["projects"]:
        query = f'"{p["name"]}" Bengaluru'
        for item in fetch_rss(query, limit=5):
            file_item(item, general_bucket, project_hint_pids=[p["id"]])
        time.sleep(1)

    # 2. Per-authority search
    for authority, queries in AUTHORITY_QUERIES.items():
        bucket = authority_updates.get(authority, [])
        for q in queries:
            for item in fetch_rss(q, limit=MAX_ITEMS_PER_AUTHORITY):
                file_item(item, bucket)
        authority_updates[authority] = bucket[:MAX_ITEMS_PER_AUTHORITY]
        time.sleep(1)

    # 3. General keyword sweep — catches new/renamed projects not yet on file
    for q in GENERAL_KEYWORDS:
        for item in fetch_rss(q, limit=8):
            file_item(item, general_bucket)
        time.sleep(1)

    # 4. Authority site notices (land acquisition, tenders)
    for item in fetch_authority_notices():
        file_item(item, general_bucket)

    news_by_project["_authorities"] = {k: v[:MAX_ITEMS_PER_AUTHORITY] for k, v in authority_updates.items()}
    news_by_project["_general"] = general_bucket[:30]
    news_by_project["_meta"] = {
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_count": len(projects_data["projects"]),
    }

    save_json(NEWS_FILE, news_by_project)
    save_json(SEEN_FILE, seen)
    log.info("Pass complete: %d new items. road_news.json updated.", new_count)


def main():
    ap = argparse.ArgumentParser(description="Bengaluru road-infra news parser")
    ap.add_argument("--once", action="store_true", help="Run a single pass and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL_MINUTES,
                     help="Minutes between passes when looping (default: %(default)s)")
    args = ap.parse_args()

    if args.once:
        run_pass()
        return

    log.info("Starting continuous road-infra parser loop — every %d minutes. Ctrl+C to stop.", args.interval)
    while True:
        try:
            run_pass()
        except Exception:
            log.exception("Pass failed, will retry next cycle")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()