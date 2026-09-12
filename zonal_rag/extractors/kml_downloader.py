"""Download KML/KMZ (and dataset-page) sources to sources/kml/."""

import io
import time
import zipfile
from pathlib import Path

import requests

from extractors.pdf_downloader import USER_AGENT, resolve_ckan_resource_urls

KML_DIR = Path("sources/kml")


def download_kml(url: str, filename: str) -> Path:
    KML_DIR.mkdir(parents=True, exist_ok=True)
    dest = KML_DIR / filename
    if dest.exists():
        return dest

    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
        timeout=300,
        stream=True,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    raw = resp.content

    is_kmz = filename.lower().endswith(".kmz") or "zip" in content_type or raw[:2] == b"PK"
    if is_kmz:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError(f"No .kml file found inside KMZ archive: {filename}")
            raw = zf.read(kml_names[0])

    dest.write_bytes(raw)
    time.sleep(1)
    return dest


def resolve_kml_dataset_urls(
    dataset_url_or_slug: str,
    name_contains: list[str] | None = None,
    name_excludes: list[str] | None = None,
) -> list[str]:
    return resolve_ckan_resource_urls(
        dataset_url_or_slug,
        extensions=(".kml", ".kmz"),
        name_contains=name_contains,
        name_excludes=name_excludes,
    )
