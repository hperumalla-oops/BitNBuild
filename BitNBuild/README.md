# Bengaluru Ward Atlas

Civic reports, metro stations and road-infrastructure projects rendered onto a
map of Bengaluru's 369 GBA wards.

## Layout

```
parsers/   scripts that scrape, parse and join
data/      source inputs — hand-authored or downloaded, not generated
output/    everything the parsers produce; web/ reads from here
web/       the map itself
```

## Running

From the **repo root**, one process serves the map, the data and the chat:

```bash
python -m uvicorn server.app:app --port 8000 --reload
```

Then open <http://localhost:8000/web/map.html>. See the
[root README](../README.md) for the full setup order.

For the map alone, a static server over **this** folder also works — not over
`web/`, because the page reads `../output/` and `../data/`:

```bash
cd BitNBuild && python -m http.server 8000
```

Either way it must be served over HTTP: opening `map.html` as a `file://` URL
does not work, because browsers block `fetch()` from local files.

Useful URL parameters: `?ward=1:42`, `?station=cubbon_park`,
`?project=ejipura_flyover`, `?metric=reportDensity`, `?view=table`,
`?theme=light|dark`.

## The pipeline

```
nammakasa.in ──scrape_nammakasa.py──> things.html ──parse_things.py──> output/things.json ─┐
Google News  ──parser.py───────────────────────────────────────────> output/news.json ─────┤
Google News  ──parser_road.py──────────────────────────────────────> output/road_news.json ┤
                                                                                           │
data/*.geojson, data/*.json (sources) ─────────────────────────────────────────────────────┤
                                                                                           ▼
                                                                            build_map_data.py
                                                                                           │
                                                          output/wards.geojson + points.json
                                                                                           │
                                                                                web/map.html
```

Rebuild what the map reads, any time a source or parser output changes:

```bash
python parsers/build_map_data.py
```

Refresh the news feeds (network; a few minutes each, politely rate-limited):

```bash
python parsers/parser.py --once        # metro station news  -> output/news.json
python parsers/parser_road.py --once   # road project news   -> output/road_news.json
python parsers/build_map_data.py       # fold them into points.json
```

Re-scrape the civic reports (needs Selenium and a local Chrome):

```bash
python parsers/scrape_nammakasa.py --html-output things.html
python parsers/parse_things.py things.html output/things.json
python parsers/build_map_data.py
```

Dependencies come from the shared environment at the repo root
(`requirements.txt`, `.venv/`) — see the [root README](../README.md).
`build_map_data.py` is standard library only.

## Supabase

The SQL lives at the repo root in [`migrations/`](../migrations/), because this
project shares a Supabase instance with `zonal_rag`:

```bash
# run migrations/002_ward_atlas.sql in the Supabase SQL editor, then:
python parsers/load_supabase.py
# then run migrations/verify_ward_atlas.sql to check the load
```

## How the datasets join

The wards come from three incompatible delimitations. The join that holds
everything together is `(corporation_id, ward_id)`:

| Source | Records | Joins by |
|---|---|---|
| `bengaluru-gba-wards-2025.geojson` | 369 wards, the geometry | — |
| `civic-database.json` | 369 wards | `(corporationId, wardId)` — exact, 369/369 |
| `things.json` | 351 wards | `(zone, ward_number)` — all 351 land inside |
| `bbmp_243_wards_mp_mla.json` | 243 wards, MP/MLA names | ward name — partial (123) |
| `bbmp_wards.json` | 198 old BBMP wards, census | ward name — partial (104) |

The last two are the older delimitations and only partly overlap the current
wards, so they enrich where they match and are simply absent elsewhere.

Things worth knowing before editing the build:

- **`bengaluru-gba-wards-2025.geojson` stores coordinates as `[lat, lng]`**,
  the reverse of the GeoJSON spec. `build_map_data.py` swaps them. Note that
  `bbmp_wards.json` uses the correct `[lng, lat]` order — the two disagree.
- 19 of its features are a `GeometryCollection` holding a polygon plus a stray
  `LineString` (a KML export artifact); the build keeps the polygons.
- 18 wards have no entry in `things.json`. They render as "No data", which is
  deliberately distinct from a ward with zero reports.
- Only 4 wards carry itemised reports; the other 347 have counts only.
- 12 metro stations (Electronic City, Bommasandra, the airport pair, Madavara)
  fall outside every ward — they are genuinely beyond the layer's extent.
- `stations.json` holds 128 entries but only **126 stations**: Majestic and
  RV Road are each listed once per line. The build merges them, so a station
  id is unique and carries every line it serves.
- **Metro routes are drawn from a computed running order.** `stations.json`
  lists each line in order, but an interchange appears once, under its *first*
  line — so filtering by line leaves it in the wrong slot (Pink jumped 6.6 km
  at MG Road, Blue 13.8 km at Nagawara). `build_map_data.py` keeps the stations
  whose first line is the one being drawn and inserts the rest where they add
  the least path length. Worst gap afterwards is 2.4 km on the city lines; the
  4.7 km on Blue is the real airport-corridor spacing.
- **`representative` in `things.json` is the sitting MLA**, not a corporator.
  It agrees with the 243-ward list on 114 of the 118 wards carrying both, and
  the 4 exceptions are spelling variants of one person (Byrathi Suresh =
  B. S. Suresha, Hebbal). It covers 350 wards to that list's 123, so the ward
  panel prefers it and falls back to `mlaName`.
- Ward areas are computed from the polygons, since the GBA layer has population
  but no area field. They drive the per-sq-km metrics.

## MLA and MP photographs

`data/mla_photos/` holds 32 headshots, slugified from the names they depict.
`build_map_data.py` matches them to the people named in the data — ignoring
honorifics, punctuation and case — and writes `web/mla-photos.json`, a map from
the **exact** name string the panel displays to a filename. The browser does a
plain dictionary lookup, so the name-matching rules live in one place and
cannot drift.

Coverage: 355 of 369 wards show an MLA photo, 123 show an MP photo. The other
14 wards have no MLA named at all, so the card is omitted. A name with no photo
falls back to initials, and so does a photo that fails to load.

The photos are referenced as `../data/mla_photos/…`, which resolves the same
way whether the FastAPI server or a plain `python -m http.server` inside
`BitNBuild/` is serving.

## Notes

- `data/leaflet-map.json` duplicates `stations.json` and has a corrupt byte at
  offset 2390. Nothing reads it; it is a candidate for deletion.
- `web/metro_map.html` is an earlier standalone experiment, kept for reference.
- `output/` is committed so the map runs without re-running any parser.
