#!/usr/bin/env bash
# Assemble the static half of the site into public/, which vercel.json names as
# the outputDirectory. Everything here is served straight off Vercel's CDN, so
# 2.4 MB of GeoJSON and 3.5 MB of photographs never pass through the Python
# function.
#
# The layout mirrors what map.html expects: the page lives at /web/map.html and
# reads ../output/ and ../data/, so those must sit beside web/ exactly as they
# do in BitNBuild/.
set -euo pipefail

rm -rf public
mkdir -p public/web public/output public/data/mla_photos

cp -r BitNBuild/web/.            public/web/
cp -r BitNBuild/data/mla_photos/. public/data/mla_photos/

# Only the two files the map actually fetches — the rest of output/ is parser
# state and intermediate JSON that the browser never asks for.
cp BitNBuild/output/wards.geojson public/output/
cp BitNBuild/output/points.json   public/output/

# config.js is generated from a local .env and is gitignored, so it is never
# present in a deployed build. /api/config.js serves it from env vars instead;
# drop any stray copy so it cannot shadow that with stale credentials.
rm -f public/web/config.js

echo "public/ assembled:"
du -sh public/web public/output public/data 2>/dev/null || true
