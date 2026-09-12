-- ============================================================================
-- Run after  python parsers/load_supabase.py
--
-- One statement on purpose: the Supabase SQL editor only shows the result of
-- the last statement, so splitting this up hides most of the report.
--
-- Every row should read 'ok'. Rows marked 'info' have no expected value.
-- If everything is 0, the loader has not run yet.
--
-- stations is 126, not the 128 entries in stations.json: two interchanges
-- (Majestic, RV Road) are listed once per line there and are merged into
-- one station each by build_map_data.py. Their lines survive in
-- station_lines, which is still 133.
-- ============================================================================

with expected(item, expected) as (
  values
    ('a. table: wards',                     369),
    ('a. table: ward_status_counts',         19),
    ('a. table: ward_reports',              491),
    ('a. table: metro_lines',                 5),
    ('a. table: stations',                  126),
    ('a. table: station_lines',             133),
    ('a. table: infra_projects',             16),
    ('a. table: news_items',                310),
    ('a. table: news_links',                319),
    ('b. wards present in report dataset',  351),
    ('b. wards with itemised reports',        4),
    ('b. stations outside any ward',         12),
    ('b. stations located in a ward',        114),
    ('b. projects with a line geometry',      8),
    ('b. projects with a point geometry',     8),
    ('c. invalid ward geometries',            0),
    ('c. wards missing a centroid',           0),
    ('c. wards with null geometry',           0),
    ('d. api: wards_geojson features',      369),
    ('d. api: points_json stations',        126),
    ('d. api: points_json projects',         16),
    ('d. api: points_json metro lines',       5)
),
actual(item, actual) as (
            select 'a. table: wards',                  count(*)::int from public.wards
  union all select 'a. table: ward_status_counts',     count(*)::int from public.ward_status_counts
  union all select 'a. table: ward_reports',           count(*)::int from public.ward_reports
  union all select 'a. table: metro_lines',            count(*)::int from public.metro_lines
  union all select 'a. table: stations',               count(*)::int from public.stations
  union all select 'a. table: station_lines',          count(*)::int from public.station_lines
  union all select 'a. table: infra_projects',         count(*)::int from public.infra_projects
  union all select 'a. table: news_items',             count(*)::int from public.news_items
  union all select 'a. table: news_links',             count(*)::int from public.news_links

  union all select 'b. wards present in report dataset',
                   (count(*) filter (where has_reports))::int from public.wards
  union all select 'b. wards with itemised reports',
                   count(distinct ward_key)::int from public.ward_reports
  union all select 'b. stations outside any ward',
                   (count(*) filter (where ward_key is null))::int from public.stations
  union all select 'b. stations located in a ward',
                   (count(*) filter (where ward_key is not null))::int from public.stations
  union all select 'b. projects with a line geometry',
                   (count(*) filter (where geometrytype(geom) = 'LINESTRING'))::int
                   from public.infra_projects
  union all select 'b. projects with a point geometry',
                   (count(*) filter (where geometrytype(geom) = 'POINT'))::int
                   from public.infra_projects

  union all select 'c. invalid ward geometries',
                   (count(*) filter (where not st_isvalid(geom)))::int from public.wards
  union all select 'c. wards missing a centroid',
                   (count(*) filter (where centroid is null))::int from public.wards
  union all select 'c. wards with null geometry',
                   (count(*) filter (where geom is null))::int from public.wards

  -- include_reports = false keeps this check cheap
  union all select 'd. api: wards_geojson features',
                   coalesce(jsonb_array_length(public.wards_geojson(0, false) -> 'features'), 0)
  union all select 'd. api: points_json stations',
                   coalesce(jsonb_array_length(public.points_json() -> 'stations'), 0)
  union all select 'd. api: points_json projects',
                   coalesce(jsonb_array_length(public.points_json() -> 'projects'), 0)
  union all select 'd. api: points_json metro lines',
                   (select count(*)::int from jsonb_object_keys(public.points_json() -> 'metroLines'))

  -- informational: no expected value, just eyeball these
  union all select 'e. info: wards whose centroid is outside their polygon',
                   (count(*) filter (where not st_within(centroid::geometry, geom)))::int
                   from public.wards
  union all select 'e. info: distinct news sources',
                   count(distinct source)::int from public.news_items
)
select a.item,
       e.expected,
       a.actual,
       case when e.expected is null      then 'info'
            when e.expected = a.actual   then 'ok'
            else 'MISMATCH'
       end as status
from actual a
left join expected e using (item)
order by a.item;
