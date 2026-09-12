# Bangalore Zoning Data Ingestion Engine

## What this is

A static data ingestion pipeline that downloads regulatory documents (PDFs, KMLs) from known Bangalore urban planning sources, extracts their content, and outputs two kinds of data:

1. **Tabular data** — structured lookups for computation (FAR tables, setback matrices, permitted use grids, parking norms), written to **Supabase Postgres** tables
2. **RAG chunks** — chunked regulatory text with metadata + embeddings for retrieval-augmented generation, written to a **Supabase Postgres + pgvector** table

Spatial data (cadastral parcels, land use zones) is unaffected by this and stays as local **GeoJSON** files — only tabular and RAG outputs live in Postgres.

This is a demo prototype. It runs once against a fixed set of URLs, produces output (DB rows + GeoJSON files), and exits. No servers, no cron jobs, no long-running APIs.

If a source document can't be downloaded or extracted (dead URL, network failure, unparseable PDF), the pipeline falls back to LLM-fabricated **synthetic placeholder data** for that source so the demo stays runnable end-to-end. Synthetic rows/chunks are always flagged with `is_synthetic = true` and must never be treated as real regulatory data downstream.

## Tech stack

- Python 3.11+ (developed against 3.12)
- `requests` for downloading files
- `pdfplumber` for PDF text extraction
- `fastkml` / `lxml` for KML ingestion (raw XML parsing via `lxml.etree.iterparse` for large files)
- `openai` SDK for LLM-based structured extraction from PDF text and for embeddings — model `gpt-5.4-mini` for extraction/synthetic-fallback generation, `text-embedding-3-small` for RAG embeddings
- `supabase` (supabase-py) client for all tabular and RAG reads/writes, using the service-role key
- Supabase Postgres + the `pgvector` extension is the database for tabular and RAG data — no local JSON/JSONL for those. Spatial outputs remain local GeoJSON files.
- Install: `pip install -r requirements.txt`

## Project structure

Shared repo-level config (`.env`, `.venv/`, `requirements.txt`, `.python-version`,
`migrations/`) lives at the repo root and is shared with the BitNBuild map
project, which uses the same Supabase instance. Paths below are repo-relative.

```
<repo root>/
├── .env                   # OPENAI_API, SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_PUBLISHABLE_KEY
├── .env.example
├── .venv/                 # one shared environment for both subprojects
├── requirements.txt       # both subprojects' dependencies
├── migrations/
│   ├── 001_zoning_rag.sql # this project: pgvector ext, all tables, match_rag_chunks() RPC
│   └── 002_ward_atlas.sql # the BitNBuild map project — no table-name overlap
└── zonal_rag/
├── CLAUDE.md              # this file
├── ingest.py              # main entry point — runs all pipelines, holds SOURCES registry
├── sources/               # downloaded raw files cached here
│   ├── pdfs/
│   └── kml/
├── extractors/
│   ├── __init__.py
│   ├── pdf_downloader.py  # download PDFs from source URLs
│   ├── kml_downloader.py  # download KMLs from source URLs
│   ├── pdf_extractor.py   # extract raw text from PDFs using pdfplumber
│   ├── table_extractor.py # OpenAI structured extraction from PDF text → row dicts (+ synthetic fallback)
│   ├── rag_chunker.py     # chunk regulatory text into RAG-ready segments + OpenAI embeddings (+ synthetic fallback)
│   ├── kml_parser.py      # parse KML into GeoJSON features
│   └── db.py              # Supabase client + delete-then-insert write helpers
├── output/
│   └── spatial/           # the only remaining flat-file output — GeoJSON
│       ├── cadastral_parcels.geojson
│       ├── land_use_residential.geojson
│       └── land_use_other.geojson
└── schemas/
    └── output_schemas.py  # pydantic models for tabular rows + RAG chunks (validated before insert)
```

Tabular data lives in Postgres tables named after the old JSON files (`permitted_uses`, `far_rules`, `setback_rules`, `height_rules`, `parking_rules`, `coverage_rules`, `plot_size_rules`, `density_rules`), plus `table_extraction_runs` (per-run metadata, replacing the old JSON `metadata` header), `rag_chunks` (RAG text + pgvector embeddings), and `sources` (the SOURCES registry mirrored into Postgres, upserted at the start of every run — powers PDF deep-linking, see below). See `migrations/001_zoning_rag.sql` for exact columns.

### PDF deep-linking

Every tabular row carries `source_id` + `source_page`; every `rag_chunks` row carries `source_id` + `page_number`. Turn either pair into a link straight to that page of the original PDF with the `pdf_link(source_id, page)` Postgres function — e.g. `select *, pdf_link(source_id, source_page) as pdf_url from far_rules`. It looks up the source's URL in the `sources` table and appends `#page=N`, which every major browser's built-in PDF viewer honors natively (confirmed: these source URLs serve `Content-Type: application/pdf` directly, not an HTML wrapper, so the fragment works with no extra hosting). Returns null when the page is null (e.g. rows extracted without a page reference, or spatial data).

## Source registry

Every source has an ID, a URL, a type, and a target (table, rag, or spatial). The pipeline iterates over this registry.

```python
SOURCES = [
    # ========================================================
    # C. Zoning Rule Store (PDFs → tables + RAG)
    # ========================================================
    {
        "id": "rmp_2015_zonal_regulations",
        "filename": "rmp_2015_zonal_regulations.pdf",
        "url": "https://data.opencity.in/dataset/3ba2ed19-5d9a-4c1a-999c-f48f7574684d/resource/dc23b4ae-e020-4499-b18d-8e4ed2f86a24/download/35d71c70-13b7-457d-a123-5a84987f67a6.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "RMP 2015 Zonal Regulations — core zoning rules for all 14 zones across 3 rings",
        "tables_to_extract": [
            "permitted_uses",    # Tables 1-6: zone × use_type → permitted/ancillary/no
            "far_rules",         # zone × ring × road_width → FAR value
            "setback_rules",     # building_height × plot_size × road_width → setback dimensions
            "height_rules",      # zone × ring × road_width → max height + max floors
            "parking_rules",     # use_type → ECS per unit or per sq.m
            "coverage_rules",    # zone × ring → max ground coverage %
            "plot_size_rules",   # zone × ring → minimum plot size for building permission
            "density_rules"      # zone × ring → max dwelling units per hectare
        ],
        "rag_sections": [
            "definitions",
            "general_notes",
            "non_conforming_uses",
            "subdivision_regulations",
            "tdr_provisions",
            "special_zone_provisions"
        ]
    },
    {
        "id": "building_byelaws_2017",
        "filename": "building_byelaws_2017.pdf",
        "url": "https://bpas.bbmpgov.in/BPAMSClient4/Downloads/Bye%20laws%20and%20Zoning%20Regulations/Model%20Building%20Byelaws%20Notification%20No.%20UDD%2014%20TTP%202017%20(P-4)%20Bengaluru,%20Dated%2028-10-2017%20.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Model Building Byelaws 2017 — building licensing, structural standards, fire safety, OC process. Primarily RAG (procedural/qualitative), not tables.",
        "tables_to_extract": [],
        "rag_sections": [
            "building_license_process",
            "structural_requirements",
            "fire_safety",
            "occupancy_certificate",
            "construction_standards",
            "penalty_violations",
            "definitions"
        ]
    },
    {
        "id": "amendment_2025_stilt_parking_draft",
        "filename": "amendment_2025_stilt_parking_draft.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/a6f22b59-9168-4140-8929-05d85eafb63a/download/9e2825c7-dc40-4bf1-a7e1-9a039b0bae4c.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Draft amendment for stilt parking, 2025. Context only — superseded by final.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_rationale"]
    },
    {
        "id": "amendment_2025_stilt_parking_final",
        "filename": "amendment_2025_stilt_parking_final_jul2025.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/de2f96fc-9e7d-4e4c-b6ff-a03c08f3b233/download/d7b00660-a3bc-49cf-b35a-3d70283ace0a.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "Final stilt parking + building height amendment, July 2025.",
        "tables_to_extract": ["height_rules_amendment"],
        "rag_sections": ["amendment_provisions"]
    },
    {
        "id": "amendment_2025_setbacks_draft",
        "filename": "amendment_2025_setbacks_draft_nov2025.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/684fb14c-fd0f-43ec-9600-f1e10bbb0368/download/revised-setback-gazette-copy.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Draft setback dimension changes, Nov 2025. Context only — superseded by final.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_rationale"]
    },
    {
        "id": "amendment_2025_setbacks_final",
        "filename": "amendment_2025_setbacks_final_jan2026.pdf",
        "url": "https://data.opencity.in/dataset/31a482fe-4ffd-4929-a61d-2fdcca90be91/resource/36f1c5e6-5f5d-4356-bfba-00c5fe1020e2/download/udd-235-mnj-2025e-05.01.2026.pdf",
        "type": "pdf",
        "targets": ["table", "rag"],
        "description": "Final setback amendment, Jan 2026 (UDD 235 MNJ 2025). In force.",
        "tables_to_extract": ["setback_rules_amendment"],
        "rag_sections": ["amendment_provisions"]
    },
    {
        "id": "amendment_2026_proposed",
        "filename": "amendment_2026_proposed_jun2026.pdf",
        "url": "https://data.opencity.in/dataset/2f0b8536-01fc-4028-8515-1209b4a78529/resource/595137ba-a6a1-460a-8de5-9262964b025a/download/proposed-amendments-rmp-2015-zonal-regulations-no.-udd-338-mnj-2026e-04.06.2026.pdf",
        "type": "pdf",
        "targets": ["rag"],
        "description": "Proposed amendment Jun 2026 (UDD 338 MNJ 2026). NOT YET IN FORCE — draft only.",
        "tables_to_extract": [],
        "rag_sections": ["amendment_provisions"]
    },

    # ========================================================
    # A. Parcel Resolver (KML → spatial)
    # ========================================================
    {
        "id": "cadastral_maps_bbmp",
        "filename": "cadastral_bbmp.kml",
        "url": "https://data.opencity.in/dataset/b5d91825-a104-41c8-bf93-3aedcfd58124/resource/9e7761c4-b39b-4124-af70-f73303e3ee6b/download/1e75032d-830a-4581-ada4-4e47a97faf87.kml",
        "type": "kml",
        "targets": ["spatial"],
        "description": "BBMP area cadastral map — parcel boundaries with survey numbers. ~70MB KML from KSRSAC.",
        "output": "cadastral_parcels.geojson"
    },

    # ========================================================
    # B. Zone Classifier (KML → spatial)
    # ========================================================
    # These are direct resource links from the land use 2017 dataset.
    # The dataset page is: https://data.opencity.in/dataset/bengaluru-urban-land-use-maps-2017
    {
        "id": "land_use_2017_residential",
        "filename": "land_use_2017_residential.kml",
        "url": "https://data.opencity.in/dataset/bengaluru-urban-land-use-maps-2017",
        "type": "kml_dataset_page",
        "targets": ["spatial"],
        "description": "KSRSAC 2017 land use — residential areas layer. Get direct KML download link from dataset page.",
        "output": "land_use_residential.geojson"
    },
    {
        "id": "land_use_2017_other",
        "filename": "land_use_2017_other.kml",
        "url": "https://data.opencity.in/dataset/bengaluru-urban-land-use-maps-2017",
        "type": "kml_dataset_page",
        "targets": ["spatial"],
        "description": "KSRSAC 2017 land use — non-residential, non-road areas. Get direct KML download link from dataset page.",
        "output": "land_use_other.geojson"
    },

    # ========================================================
    # D. Overlay Checker — no separate download.
    # Water bodies are extracted from the land use KML above.
    # Lake/drain buffer RULES are encoded from the zonal
    # regulations text (already captured in rmp_2015_zonal_regulations).
    # ========================================================
]
```

## Output schemas

### Tabular outputs (Supabase Postgres tables)

Each table row carries `source_id` (which SOURCES entry produced it) and `is_synthetic` (true only for LLM-fabricated fallback rows), in addition to the fields below. Row shapes (see `schemas/output_schemas.py` for the pydantic models, `migrations/001_zoning_rag.sql` for the DDL):

#### permitted_uses
```json
{
  "zone": "R1",
  "use_type": "Petty shops",
  "use_category": "Commercial",
  "permission": "ancillary",
  "conditions": "Only on roads >= 40ft (12m). Max 20% of sital area.",
  "source_table": "Table 1",
  "source_page": 12
}
```

#### far_rules
```json
{
  "zone": "R1",
  "ring": "I",
  "road_width_min_m": 0,
  "road_width_max_m": 9.0,
  "far": 1.75,
  "premium_far": null,
  "premium_conditions": null,
  "source_page": 18
}
```

#### setback_rules
```json
{
  "building_height_min_m": 0,
  "building_height_max_m": 8.5,
  "plot_area_min_sqm": 0,
  "plot_area_max_sqm": 60,
  "front_setback_m": 0.75,
  "rear_setback_m": 0,
  "side1_setback_m": 0,
  "side2_setback_m": 0,
  "source": "amendment_2025_setbacks_jan2026",
  "source_page": 2
}
```

#### height_rules
```json
{
  "zone": "R1",
  "ring": "II",
  "road_width_min_m": 12.0,
  "road_width_max_m": null,
  "max_height_m": 15.0,
  "max_floors": 4,
  "max_floors_description": "G+3 or Stilt+4",
  "source_page": 20
}
```

#### parking_rules
```json
{
  "use_type": "Residential apartment",
  "unit": "per dwelling unit",
  "ecs_required": 1.0,
  "visitor_parking_pct": 20,
  "source_page": 25
}
```

#### coverage_rules, plot_size_rules, density_rules
Same pattern — zone/ring keys and numeric values, one row per combination.

`height_rules_amendment` and `setback_rules_amendment` extractions land in the base `height_rules` / `setback_rules` tables (see `TABLE_NAME_ALIASES` in `schemas/output_schemas.py`), distinguished by `source_id`.

A `table_extraction_runs` row is logged per (table, source_id) extraction, standing in for the old JSON `metadata` header (`source_section`, `description`, `row_count`, `extracted_at`, `is_synthetic`).

### RAG outputs (Supabase Postgres, `rag_chunks` table + pgvector)

Each row is one chunk:

```json
{
  "chunk_id": "rmp_2015_zonal_regulations__definitions__001",
  "source_id": "rmp_2015_zonal_regulations",
  "source_url": "https://data.opencity.in/...",
  "section": "definitions",
  "clause_number": "2.1",
  "clause_title": "Amalgamation",
  "text": "Amalgamation: Combining two or more plots as a single plot.",
  "page_number": 3,
  "char_offset_start": 1200,
  "char_offset_end": 1265,
  "embedding": "[1536-dim vector from text-embedding-3-small]",
  "is_synthetic": false
}
```

Chunking rules:
- Split on clause/section boundaries first (numbered headings like "2.1", "3.16.i", etc.) — regex/heuristic, no LLM
- If a clause is longer than ~800 tokens, split on paragraph boundaries within it
- Each chunk keeps its clause number, page number, and a `section` tag classified against the source's `rag_sections` list via keyword matching (see `SECTION_KEYWORDS` in `ingest.py`)
- Each chunk is embedded with OpenAI `text-embedding-3-small` before insert
- Query with the `match_rag_chunks(query_embedding, match_threshold, match_count)` Postgres RPC defined in `migrations/001_zoning_rag.sql`

### Spatial outputs (GeoJSON)

Standard GeoJSON FeatureCollection. Each feature has:
```json
{
  "type": "Feature",
  "geometry": { "type": "Polygon", "coordinates": [...] },
  "properties": {
    "source_id": "cadastral_maps_bbmp",
    "survey_number": "45/2",
    "village": "Begur",
    "hobli": "Begur",
    "taluk": "Bangalore South",
    "area_sqm": 223.5
  }
}
```

For land use zones, properties include:
```json
{
  "land_use_class": "Residential",
  "land_use_subclass": "R1",
  "source_year": 2017
}
```

## Extraction pipeline per source type

### PDF → Tables (LLM extraction via OpenAI)

1. Download the PDF to `sources/pdfs/`
2. Extract full text using pdfplumber, page by page
3. For each `tables_to_extract` entry, send the relevant pages (5-10 at a time) to OpenAI (`gpt-5.4-mini`) with a structured extraction prompt built from the pydantic model's JSON schema
4. Accumulate rows across page chunks and deduplicate
5. Validate output against pydantic models (`schemas/output_schemas.py`); malformed rows are dropped with a warning, not fatal
6. Delete-then-insert into the matching Supabase table, scoped to `source_id` (`extractors/db.replace_table_rows`)
7. Log a `table_extraction_runs` row for the (table, source_id) pair

If the PDF can't be downloaded/parsed, or extraction yields zero usable rows after all page chunks, fall back to asking the model to fabricate a small set of plausible example rows instead — inserted with `is_synthetic = true`.

The LLM extraction prompt pattern (see `extractors/table_extractor.py::EXTRACTION_PROMPT`):

```
You are extracting structured data from Indian zoning regulations.

Here is the text from pages {start}-{end} of {source_description}:

<regulatory_text>
{extracted_text}
</regulatory_text>

Extract all rows matching this JSON schema (a list of objects with these fields):
{json_schema}

Rules:
- Extract every row you can find. Do not skip entries.
- Convert all measurements to metric (metres, sq.m). If source uses feet, convert.
- If a cell says "as per table X", leave the value as null and note the reference in a "conditions" field if the schema has one.
- Include the source page number for each row (source_page).
- Return ONLY a valid JSON array of row objects. No markdown, no explanation, no code fences.
```

### PDF → RAG chunks (regex chunking + OpenAI embeddings)

1. Same pdfplumber text extraction as above
2. Split text on section/clause boundaries using regex patterns:
   - Numbered sections: `^\d+\.\d+`, `^\d+\)`, `^[ivx]+\.`
3. Classify each chunk's `section` tag against the source's `rag_sections` list via keyword matching (`ingest.py::classify_section` / `SECTION_KEYWORDS`) — no LLM needed for chunking or classification
4. Embed each chunk's text with OpenAI `text-embedding-3-small`
5. Delete-then-insert into `rag_chunks`, scoped to `source_id`

If no source text is available at all, fall back to a handful of LLM-fabricated synthetic chunks (`extractors/rag_chunker.py::synthetic_chunks`), inserted with `is_synthetic = true`.

### KML → GeoJSON

1. Download KML to `sources/kml/`
2. Parse with fastkml or lxml (some files are large — stream parse if needed)
3. Extract each Placemark's geometry and properties
4. Convert KML geometry to GeoJSON geometry
5. Map KML ExtendedData / description fields to GeoJSON properties
6. Write to `output/spatial/{output_name}.geojson`

For the 70MB cadastral KML, use streaming XML parse — do not load the whole DOM into memory.

### Dataset pages (OpenCity CKAN)

Some sources are CKAN dataset pages, not direct file URLs. For these:
1. Fetch the dataset page HTML
2. Find the resource download links (they follow the pattern `data.opencity.in/dataset/.../resource/.../download/...`)
3. Download each PDF/KML resource
4. Process each as above

Alternatively, use the CKAN API: `https://data.opencity.in/api/3/action/package_show?id={dataset_id}` returns JSON with resource URLs.

## How to run

1. Run `migrations/001_zoning_rag.sql` once in the Supabase SQL editor (enables `pgvector`, creates all tables + the `match_rag_chunks` and `pdf_link` functions).
2. Set up `.env` (see `.env.example`): `OPENAI_API`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (service-role key — needed to bypass RLS for these writes).

```bash
cd zonal_rag
pip install -r requirements.txt
python ingest.py
```

The script auto-downloads everything. On first run it:
1. Creates `sources/pdfs/`, `sources/kml/`, and `output/spatial/`
2. Downloads every file in the SOURCES registry (skips if file already exists in `sources/`)
3. Runs PDF text extraction with pdfplumber
4. Runs OpenAI-based table extraction for sources with `tables_to_extract`, writing to Supabase (falls back to synthetic rows on failure)
5. Runs RAG chunking + embedding for sources with `rag_sections`, writing to Supabase `rag_chunks` (falls back to synthetic chunks on failure)
6. Runs KML → GeoJSON conversion for spatial sources, writing local files under `output/spatial/`
7. Prints per-source row/chunk/feature counts, flagging `[SYNTHETIC]` where the fallback path was used

Re-running skips downloads (cached) but re-runs extraction; each (table, source_id) or (rag_chunks, source_id) pair is deleted and reinserted, so re-running is idempotent per source without disturbing rows from other sources in the same table. Pass `--skip-download` to skip the download step entirely if files are already in place. Pass `--only <source_id>` to process a single source.

## Important implementation notes

- The BBMP byelaws PDF URL has URL-encoded spaces — handle this correctly in requests
- Some OpenCity PDFs may redirect — follow redirects with `allow_redirects=True` (requests default)
- KML files may use KMZ (zipped KML) — detect by extension or content-type header, unzip first
- The cadastral KML is ~70MB — don't load it all into memory, use iterative XML parsing with `lxml.etree.iterparse`
- pdfplumber sometimes garbles table text in scanned PDFs — the RMP 2015 ZR is a text PDF (not scanned), so this should work well
- The building byelaws PDF may have Kannada text mixed in — extract English text only for this prototype
- For LLM extraction and synthetic fallback generation, use the `gpt-5.4-mini` model via the OpenAI API (`chat.completions.create`, `max_completion_tokens=4096`). Retry on rate limits/transient errors with exponential backoff. Use `text-embedding-3-small` for RAG chunk embeddings.
- If a source can't be downloaded/parsed, or LLM extraction returns nothing usable, fall back to LLM-fabricated synthetic placeholder data rather than failing the run — always flagged `is_synthetic = true` so it's never confused with real regulatory data.
- LLM table extraction is not guaranteed complete or perfectly schema-fitted (e.g. it may drop an occasional row from a long table, or squeeze data that doesn't cleanly fit the target columns into a text field like `max_floors_description`). For this PoC that's an acceptable tradeoff — no automated completeness verification or re-mapping is built in. Real production use would need per-table validation against the source table's row count.
- All outputs should be UTF-8 encoded
- Page numbers in pdfplumber are 0-indexed — add 1 when recording `source_page`
- For `kml_dataset_page` type sources, the URL points to a CKAN dataset page, not a direct file. The downloader should fetch the page HTML, find all resource download links matching `.kml` or `.kmz`, and download each one. Alternatively, use the CKAN API: `https://data.opencity.in/api/3/action/package_show?id={dataset_slug}` returns JSON with `result.resources[].url` for each file.
- A CKAN dataset page can bundle several distinct layers as separate named resources all sharing the same file extension (the land-use-2017 dataset has 4: the full combined map, residential-only, minus-roads-and-residential, and roads-only — all `.kml`/`.kmz`). Extension filtering alone can't tell them apart; `resolve_ckan_resource_urls`/`resolve_kml_dataset_urls` take `name_contains`/`name_excludes` keyword lists matched against each resource's CKAN display name, and each `kml_dataset_page` SOURCES entry sets `resource_name_contains`/`resource_name_excludes` accordingly (see the `land_use_2017_residential` / `land_use_2017_other` entries in `ingest.py`). Getting this wrong silently merges the wrong layers together with no error.
- Set a reasonable `User-Agent` header (e.g. `bangalore-zoning-ingest/0.1`) and respect rate limits — add a 1-second delay between downloads.
- All Supabase writes go through the service-role key (`SUPABASE_SECRET_KEY`), bypassing RLS — this script is a trusted backend job, never expose that key client-side.