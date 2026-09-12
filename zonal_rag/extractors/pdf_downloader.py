"""Download PDF (and dataset-page) sources to sources/pdfs/."""

import re
import time
from pathlib import Path

import requests

USER_AGENT = "bangalore-zoning-ingest/0.1"
PDF_DIR = Path("sources/pdfs")


def download_pdf(url: str, filename: str) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    dest = PDF_DIR / filename
    if dest.exists():
        return dest

    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
        timeout=120,
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    time.sleep(1)
    return dest


def resolve_ckan_resource_urls(
    dataset_url_or_slug: str,
    extensions=(".pdf",),
    name_contains: list[str] | None = None,
    name_excludes: list[str] | None = None,
) -> list[str]:
    """Resolve a CKAN dataset page (or slug) to direct resource download URLs.

    A dataset page can bundle several distinct layers as separate named
    resources (e.g. "Residential Areas" vs "Minus Roads and Residential
    Areas") that all happen to be the same file type — extension alone
    can't tell them apart, so name_contains/name_excludes filter on the
    resource's display name (case-insensitive substring match).
    """
    slug_match = re.search(r"/dataset/([^/?#]+)", dataset_url_or_slug)
    slug = slug_match.group(1) if slug_match else dataset_url_or_slug

    api_url = f"https://data.opencity.in/api/3/action/package_show?id={slug}"
    resp = requests.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    urls = []
    for resource in data.get("result", {}).get("resources", []):
        resource_url = resource.get("url", "")
        if not any(resource_url.lower().endswith(ext) for ext in extensions):
            continue
        name = resource.get("name", "").lower()
        if name_contains and not any(kw.lower() in name for kw in name_contains):
            continue
        if name_excludes and any(kw.lower() in name for kw in name_excludes):
            continue
        urls.append(resource_url)
    return urls
