"""Bangalore Zoning Data Ingestion Engine — main entry point.

Downloads regulatory PDFs/KMLs, extracts tabular data (-> Supabase Postgres)
and RAG chunks (-> Supabase Postgres + pgvector), and spatial data
(-> local GeoJSON files, unchanged from the original design).

Usage:
    python ingest.py [--skip-download] [--only SOURCE_ID]
"""

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from extractors import db, kml_parser, pdf_extractor, rag_chunker, table_extractor
from extractors.kml_downloader import download_kml, resolve_kml_dataset_urls
from extractors.pdf_downloader import download_pdf, resolve_ckan_resource_urls
from schemas.output_schemas import TABLE_NAME_ALIASES, TABLE_ROW_MODELS

SOURCES = [
    # ========================================================
    # C. Zoning Rule Store (PDFs -> tables + RAG)
    # ========================================================
    {
        "id": "rmp_2015_zonal_regulations",
        "filename": "rmp_2015_zonal_regulations.pdf",
        "url": "https://data.opencity.in/dataset/3ba2ed19-5d9a-4c1a-999c-f48f7574684d/resource/dc23b4ae-e020-4499-b18d-8e4ed2f86a24/download/35d71c70-13b7-457d-a123-5a84987f67a6.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "RMP 2015 Zonal Regulations — core zoning rules for all 14 zones across 3 rings",
        "tables_to_extract": [
            "permitted_uses",
            "far_rules",
            "setback_rules",
            "height_rules",
            "parking_rules",
            "coverage_rules",
            "plot_size_rules",
            "density_rules",
        ],
        "rag_sections": [
            "definitions",
            "general_notes",
            "non_conforming_uses",
            "subdivision_regulations",
            "tdr_provisions",
            "special_zone_provisions",
        ],
    },
    {
        "id": "building_byelaws_2017",
        "filename": "building_byelaws_2017.pdf",
        "url": "https://bpas.bbmpgov.in/BPAMSClient4/Downloads/Bye%20laws%20and%20Zoning%20Regulations/Model%20Building%20Byelaws%20Notification%20No.%20UDD%2014%20TTP%202017%20(P-4)%20Bengaluru,%20Dated%2028-10-2017%20.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Model Building Byelaws 2017 — building licensing, structural standards, fire safety, OC process.",
        "tables_to_extract": [],
        "rag_sections": [
            "building_license_process",
            "structural_requirements",
            "fire_safety",
            "occupancy_certificate",
            "construction_standards",
            "penalty_violations",
            "definitions",
        ],
    },
    {
        "id": "amendment_2025_stilt_parking_draft",
        "filename": "amendment_2025_stilt_parking_draft.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/a6f22b59-9168-4140-8929-05d85eafb63a/download/9e2825c7-dc40-4bf1-a7e1-9a039b0bae4c.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Draft amendment for stilt parking, 2025. Context only — superseded by final.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_rationale"],
    },
    {
        "id": "amendment_2025_stilt_parking_final",
        "filename": "amendment_2025_stilt_parking_final_jul2025.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/de2f96fc-9e7d-4e4c-b6ff-a03c08f3b233/download/d7b00660-a3bc-49cf-b35a-3d70283ace0a.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "Final stilt parking + building height amendment, July 2025.",
        "tables_to_extract": ["height_rules_amendment"],
        "rag_sections": ["amendment_provisions"],
    },
    {
        "id": "amendment_2025_setbacks_draft",
        "filename": "amendment_2025_setbacks_draft_nov2025.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/684fb14c-fd0f-43ec-9600-f1e10bbb0368/download/revised-setback-gazette-copy.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Draft setback dimension changes, Nov 2025. Context only — superseded by final.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_rationale"],
    },
    {
        "id": "amendment_2025_setbacks_final",
        "filename": "amendment_2025_setbacks_final_jan2026.pdf",
        "url": "https://data.opencity.in/dataset/31a482fe-4ffd-4929-a61d-2fdcca90be91/resource/36f1c5e6-5f5d-4356-bfba-00c5fe1020e2/download/udd-235-mnj-2025e-05.01.2026.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "Final setback amendment, Jan 2026 (UDD 235 MNJ 2025). In force.",
        "tables_to_extract": ["setback_rules_amendment"],
        "rag_sections": ["amendment_provisions"],
    },
    {
        "id": "amendment_2026_proposed",
        "filename": "amendment_2026_proposed_jun2026.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/595137ba-a6a1-460a-8de5-9262964b025a/download/proposed-amendments-rmp-2015-zonal-regulations-no.-udd-338-mnj-2026e-04.06.2026.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Proposed amendment Jun 2026 (UDD 338 MNJ 2026). NOT YET IN FORCE — draft only.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_provisions"],
    },
    # ========================================================
    # A. Parcel Resolver (KML -> spatial, unchanged: local GeoJSON)
    # ========================================================
    {
        "id": "cadastral_maps_bbmp",
        "filename": "cadastral_bbmp.kml",
        "url": "https://data.opencity.in/dataset/b5d91825-a104-41c8-bf93-3aedcfd58124/resource/9e7761c4-b39b-4124-af70-f73303e3ee6b/download/1e75032d-830a-4581-ada4-4e47a97faf87.kml",
        "type": "kml",
        "targets": ["spatial"],
        "description": "BBMP area cadastral map — parcel boundaries with survey numbers. ~70MB KML from KSRSAC.",
        "output": "cadastral_parcels.geojson",
    },
    # ========================================================
    # B. Zone Classifier (KML -> spatial, unchanged: local GeoJSON)
    # ========================================================
    {
        "id": "land_use_2017_residential",
        "filename": "land_use_2017_residential.kml",
        "url": "https://data.opencity.in/dataset/bengaluru-urban-land-use-maps-2017",
        "type": "kml_dataset_page",
        "targets": ["spatial"],
        "description": "KSRSAC 2017 land use — residential areas layer.",
        "output": "land_use_residential.geojson",
        # The dataset page bundles 4 distinct resources (full map, residential,
        # minus-roads-and-residential, roads) all as .kml/.kmz — name filtering
        # picks out just "Land Use Maps Residential Areas 2017", not the
        # "...Minus Roads and Residential Areas..." resource (which also
        # contains the substring "residential").
        "resource_name_contains": ["residential"],
        "resource_name_excludes": ["minus"],
    },
    {
        "id": "land_use_2017_other",
        "filename": "land_use_2017_other.kml",
        "url": "https://data.opencity.in/dataset/bengaluru-urban-land-use-maps-2017",
        "type": "kml_dataset_page",
        "targets": ["spatial"],
        "description": "KSRSAC 2017 land use — non-residential, non-road areas.",
        "output": "land_use_other.geojson",
        "resource_name_contains": ["minus"],
        "resource_name_excludes": [],
    },
]

# Heuristic keyword -> rag_section classifier (regex chunking stays LLM-free).
SECTION_KEYWORDS = {
    "definitions": ["definition", "shall mean", "interpretation"],
    "general_notes": ["general note", "general provision"],
    "non_conforming_uses": ["non-conforming", "non conforming"],
    "subdivision_regulations": ["subdivision", "sub-division", "layout plan"],
    "tdr_provisions": ["transferable development right", "tdr "],
    "special_zone_provisions": ["special zone", "special economic zone", "sez", "special township"],
    "building_license_process": ["license", "licence", "sanction of building plan", "application for permit"],
    "structural_requirements": ["structural design", "foundation", "load bearing", "structural safety"],
    "fire_safety": ["fire safety", "fire fighting", "fire escape", "fire nio"],
    "occupancy_certificate": ["occupancy certificate"],
    "construction_standards": ["construction standard", "workmanship", "building material"],
    "penalty_violations": ["penalty", "violation", "unauthorised", "unauthorized", "demolition"],
}


def classify_section(clause_title: str | None, known_sections: list[str]) -> str:
    if clause_title:
        lowered = clause_title.lower()
        for section in known_sections:
            for keyword in SECTION_KEYWORDS.get(section, []):
                if keyword in lowered:
                    return section
    return known_sections[0]


async def _extract_and_write_table(source: dict, table_key: str, pages: list[dict]) -> None:
    source_id = source["id"]
    model = TABLE_ROW_MODELS[table_key]
    actual_table = TABLE_NAME_ALIASES.get(table_key, table_key)
    rows, is_synthetic = await table_extractor.extract_table_rows(
        table_name=actual_table,
        json_schema=model.model_json_schema(),
        source_description=source["description"],
        pages=pages,
    )
    validated = []
    for row in rows:
        try:
            validated.append(model(**row).model_dump())
        except Exception as e:
            print(f"  [warn] dropping malformed {table_key} row: {e}")
    count = db.replace_table_rows(actual_table, source_id, validated, is_synthetic=is_synthetic)
    db.log_extraction_run(
        table_name=actual_table,
        source_id=source_id,
        source_section=table_key,
        description=source["description"],
        row_count=count,
        is_synthetic=is_synthetic,
    )
    flag = " [SYNTHETIC]" if is_synthetic else ""
    print(f"  table {actual_table}: {count} rows{flag}")


async def _extract_and_write_rag(source: dict, pages: list[dict]) -> None:
    source_id = source["id"]
    rag_sections = source["rag_sections"]
    raw_chunks = []
    if pages:
        raw_chunks = rag_chunker.chunk_pages(pages, source_id, section="")

    if raw_chunks:
        section_counters: dict[str, int] = {}
        for chunk in raw_chunks:
            section = classify_section(chunk["clause_title"], rag_sections)
            chunk["section"] = section
            section_counters[section] = section_counters.get(section, 0) + 1
            chunk["chunk_id"] = f"{source_id}__{section}__{section_counters[section]:03d}"
        embedded = await rag_chunker.embed_chunks(raw_chunks, source["url"])
        count = db.replace_rag_chunks(source_id, embedded, is_synthetic=False)
        print(f"  rag: {count} chunks")
    else:
        # No pages, or pages had no extractable text (e.g. a scanned PDF) —
        # same "real extraction yielded nothing usable" fallback as tables.
        results = await asyncio.gather(
            *(
                rag_chunker.synthetic_chunks(source_id, source["url"], section, source["description"])
                for section in rag_sections
            )
        )
        all_synthetic = [chunk for section_chunks in results for chunk in section_chunks]
        count = db.replace_rag_chunks(source_id, all_synthetic, is_synthetic=True)
        print(f"  rag: {count} chunks [SYNTHETIC]")


async def process_pdf_source(source: dict, skip_download: bool) -> None:
    source_id = source["id"]
    print(f"\n=== {source_id} ({source['description']}) ===")

    pages: list[dict] = []
    if not skip_download:
        try:
            pdf_path = download_pdf(source["url"], source["filename"])
            pages = pdf_extractor.extract_pages(pdf_path)
        except Exception as e:
            print(f"  [warn] download/extract failed for {source_id}: {e}")
            pages = []
    else:
        pdf_path = Path("sources/pdfs") / source["filename"]
        if pdf_path.exists():
            pages = pdf_extractor.extract_pages(pdf_path)
        else:
            print(f"  [warn] --skip-download but {pdf_path} not found")

    # Every table extraction and the RAG chunk/embed pipeline for this source
    # run concurrently (bounded by table_extractor.MAX_CONCURRENT_CALLS),
    # instead of one OpenAI call at a time.
    tasks = []
    if "table" in source["targets"]:
        for table_key in source["tables_to_extract"]:
            tasks.append(_extract_and_write_table(source, table_key, pages))
    if "rag" in source["targets"]:
        tasks.append(_extract_and_write_rag(source, pages))

    if tasks:
        await asyncio.gather(*tasks)


def process_kml_source(source: dict, skip_download: bool) -> None:
    source_id = source["id"]
    print(f"\n=== {source_id} ({source['description']}) ===")
    output_path = Path("output/spatial") / source["output"]

    # kml_dataset_page sources save under "{source_id}_{i}.kml" (resolved at
    # runtime from the CKAN API), not source["filename"] — only direct "kml"
    # sources have a fixed filename to check up front. download_kml() itself
    # already skips any resource that's already on disk either way.
    if skip_download and source["type"] == "kml" and not (Path("sources/kml") / source["filename"]).exists():
        print(f"  [warn] --skip-download but source file not found, skipping")
        return

    try:
        if source["type"] == "kml_dataset_page":
            urls = resolve_kml_dataset_urls(
                source["url"],
                name_contains=source.get("resource_name_contains"),
                name_excludes=source.get("resource_name_excludes"),
            )
            all_features = []
            for i, url in enumerate(urls):
                fname = f"{source['id']}_{i}.kml"
                kml_path = download_kml(url, fname)
                geojson = kml_parser.kml_to_geojson(kml_path, source_id)
                all_features.extend(geojson["features"])
            result = {"type": "FeatureCollection", "features": all_features}
        else:
            kml_path = download_kml(source["url"], source["filename"])
            result = kml_parser.kml_to_geojson(kml_path, source_id)

        kml_parser.write_geojson(result, output_path)
        print(f"  spatial: {len(result['features'])} features -> {output_path}")
    except Exception as e:
        print(f"  [warn] KML processing failed for {source_id}: {e}")
        traceback.print_exc()


async def _run(sources: list[dict], skip_download: bool) -> None:
    # Populate the `sources` lookup table so pdf_link(source_id, page) can
    # resolve a clickable deep-link to the exact page of the original PDF.
    db.upsert_sources(sources)

    # All sources share one event loop (and therefore one OpenAI semaphore)
    # for the whole run — creating a fresh loop per source would leave the
    # module-level semaphore bound to a closed loop after the first source.
    for source in sources:
        try:
            if source["type"] == "pdf":
                await process_pdf_source(source, skip_download)
            elif source["type"] in ("kml", "kml_dataset_page"):
                process_kml_source(source, skip_download)
        except Exception as e:
            print(f"[error] {source['id']} failed: {e}")
            traceback.print_exc()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    sources = SOURCES
    if args.only:
        sources = [s for s in SOURCES if s["id"] == args.only]
        if not sources:
            print(f"No source with id '{args.only}'")
            sys.exit(1)

    asyncio.run(_run(sources, args.skip_download))

    print("\nDone.")


if __name__ == "__main__":
    main()
