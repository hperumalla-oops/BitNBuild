# BnB — Bengaluru civic data

Two subprojects that share one Python environment and one Supabase instance.

| | |
|---|---|
| **[BitNBuild/](BitNBuild/)** | Ward Atlas — civic reports, metro stations and road projects on a map of Bengaluru's 369 GBA wards. See [BitNBuild/README.md](BitNBuild/README.md). |
| **[zonal_rag/](zonal_rag/)** | Zoning Data Ingestion Engine — PDF/KML extraction into Postgres tables plus a pgvector RAG index. See [zonal_rag/CLAUDE.md](zonal_rag/CLAUDE.md). |

## Layout

```
.env  .env.example       shared credentials — OpenAI, Supabase, Tavily
.python-version          3.12.10 (pyenv reads this for the whole tree)
.venv/                   one environment for both subprojects
requirements.txt         both subprojects' dependencies
migrations/              SQL for the shared Supabase database, in order
server/                  FastAPI — serves the web app and the chat agent
BitNBuild/               the map and chat UI
zonal_rag/               the ingestion engine
```

## Running it

```bash
python -m uvicorn server.app:app --port 8000 --reload
```

Then open **<http://localhost:8000/web/map.html>**.

Run it from the **repo root**, not from `server/`. One process serves
everything:

| Route | What |
|---|---|
| `/web/map.html` | the map and chat UI |
| `/output/*` | the generated data files (the map's offline fallback) |
| `/data/*` | source data, including the MLA/MP photographs |
| `/api/chat` | the chat agent |
| `/api/health` | which credentials the server found — check this first if chat is dead |

The **OpenAI and Tavily keys stay inside this process**. Unlike the Supabase
publishable key they are secret and must never reach the browser; that is the
whole reason the server exists.

Opening `map.html` as a `file://` URL will not work — browsers block `fetch()`
from local files.

### Map only, without the chat

The map runs off a plain static server if you do not need the chat tab. Serve
`BitNBuild/` (not `BitNBuild/web/`, because the page reads `../output/` and
`../data/`):

```bash
cd BitNBuild && python -m http.server 8000
```

## Setup

From scratch, in order:

```bash
# 1. environment
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 2. credentials — fill in OpenAI, Supabase and Tavily keys
cp .env.example .env

# 3. database: run migrations/001, 002 and 003 in the Supabase SQL editor

# 4. build the map's data files and push them to Supabase
python BitNBuild/parsers/build_map_data.py
python BitNBuild/parsers/load_supabase.py

# 5. point the map at Supabase (omit this and it reads the local files)
python BitNBuild/parsers/make_web_config.py

# 6. run
python -m uvicorn server.app:app --port 8000 --reload
```

Step 4 is optional if you only want the map: it already works from the
committed files in `BitNBuild/output/`. Steps 3–5 are what the chat needs.

## Database

Both subprojects write to the **same Supabase project**. Their tables do not
overlap, so the migrations are independent and can run in either order — but
run them numbered, so a fresh database ends up in a known state:

```
migrations/001_zoning_rag.sql    pgvector, zoning tables, match_rag_chunks() RPC
migrations/002_ward_atlas.sql    PostGIS, ward/station/project/news tables + RPCs
migrations/003_chat_api.sql      spatial + ranking RPCs the chat agent calls
migrations/verify_ward_atlas.sql read-only check, run after loading the map data
```

`002_ward_atlas.sql` opens with a destructive reset block that drops its own
tables. It touches nothing belonging to `001`.

Loading data:

```bash
python BitNBuild/parsers/build_map_data.py    # regenerate the map's two files
python BitNBuild/parsers/load_supabase.py     # push them into Supabase
python zonal_rag/ingest.py                    # run the zoning pipelines
```

## Where the map reads from

`BitNBuild/web/config.js` (generated from `.env` by
`parsers/make_web_config.py`, gitignored) decides it. Present, and the map
calls the Supabase RPCs `wards_geojson()` and `points_json()`; absent, it falls
back to the generated files in `BitNBuild/output/`, so it still works offline.
Both return the identical shape.

## Deploying to Vercel

The repo is set up for it. Push to GitHub, import the project in Vercel, and
set these five environment variables (Project Settings → Environment
Variables):

```
OPENAI_API                 server-side only
SUPABASE_URL
SUPABASE_SECRET_KEY        server-side only — bypasses RLS
SUPABASE_PUBLISHABLE_KEY   sent to the browser by /api/config.js
TAVILY_API_KEY             server-side only
```

No other configuration is needed — `vercel.json` carries the rest:

| | |
|---|---|
| `scripts/vercel-build.sh` | assembles `public/` — the map, the two data files and the photographs, served straight off the CDN so 6 MB never passes through the function |
| `api/index.py` | re-exports the same FastAPI app `uvicorn` runs locally |
| `maxDuration: 60` | a chat turn measured 3.6–14.6s locally; Vercel's 10s default would cut the long ones off |
| `/api/config.js` | the browser's Supabase credentials, from env vars — a generated, gitignored `config.js` cannot exist in a deployed build |

**Before you deploy, read this.** The chat endpoint is public, and your OpenAI
key sits behind it — anyone with the URL can spend your credits. There is a
per-IP rate limit (`CHAT_RATE_LIMIT`, default 20 per 5 minutes), but on
serverless each instance keeps its own counter and cold starts reset it, so
treat it as a speed bump, not access control. For anything beyond a short-lived
demo, turn on Vercel **Deployment Protection**.

Local development is unchanged: `uvicorn` still serves everything, and a local
`config.js` from `make_web_config.py` still works (it loads first, and
`/api/config.js` overrides it when present).

## The chat agent

Two hard-scoped tool groups; the toggle in the UI decides which the model is
even shown, so it cannot answer a zoning question out of the map tables.

| Mode | Tools |
|---|---|
| Zoning | `zoning_search` (2,629 embedded chunks), `zoning_rules` (8 rule tables), `zoning_sources` |
| Map | `ward_lookup`, `ward_rank`, `ward_reports`, `ward_at_point`, `stations_near`, `station_lookup`, `projects_in_ward` |
| Both | `web_search` (Tavily) |

Notes on the design:

- **Vector search is bad at exact numbers.** The prompt pushes any question
  with a figure in it (FAR, setback, height) to the structured rule tables, and
  keeps `zoning_search` for wording and definitions.
- **No SQL is ever built from model output.** Table and metric names are
  whitelisted in `server/tools.py`; everything else travels as a parameter.
- **Answers carry links.** Zoning answers cite a `pdf_url` deep-linked to the
  source page; map answers return a `map_link` that opens that ward, station or
  project on the map tab.
- **Conversations live in the browser** (`localStorage`), and the whole thread
  is posted each turn — the server keeps no state.
