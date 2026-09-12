-- ============================================================================
-- Bengaluru Ward Atlas — Supabase schema
--
-- Run this once in the Supabase SQL editor (or psql). It is re-runnable: the
-- DROP block at the top resets everything, so re-running DESTROYS loaded data
-- and you must re-run the loader afterwards.
--
-- After this, load data with:  python parsers/load_supabase.py
-- ============================================================================

create extension if not exists postgis;

-- ---------------------------------------------------------------------------
-- Reset (destructive — remove this block once you have data you care about)
-- ---------------------------------------------------------------------------
drop function if exists public.wards_geojson(double precision, boolean) cascade;
drop function if exists public.points_json() cascade;
drop function if exists public._news_for(text, text) cascade;
drop function if exists public.import_wards(jsonb) cascade;
drop function if exists public.import_points(jsonb) cascade;
drop function if exists public._int(text) cascade;
drop function if exists public._num(text) cascade;
drop function if exists public._date(text) cascade;
drop function if exists public._bool(text) cascade;
drop function if exists public._geom(jsonb) cascade;
drop view if exists public.ward_summary cascade;
drop table if exists public.news_links cascade;
drop table if exists public.news_items cascade;
drop table if exists public.station_lines cascade;
drop table if exists public.stations cascade;
drop table if exists public.metro_lines cascade;
drop table if exists public.infra_projects cascade;
drop table if exists public.ward_reports cascade;
drop table if exists public.ward_status_counts cascade;
drop table if exists public.wards cascade;


-- ---------------------------------------------------------------------------
-- wards — 369 GBA 2025 wards, the spine everything else joins to
-- ---------------------------------------------------------------------------
create table public.wards (
  ward_key                    text primary key,          -- '5:25' = corporation:ward
  corporation_id              smallint not null,
  ward_id                     smallint not null,
  corporation                 text not null,
  ward_name                   text not null,
  ward_name_kn                text,
  zone_name                   text,
  assembly                    text,
  ro_division                 text,
  aro_sub_division            text,

  -- demographics
  population                  integer,
  pop_male                    integer,
  pop_female                  integer,
  pop_sc                      integer,
  pop_st                      integer,
  area_sq_km                  numeric(10,3),             -- computed from the polygon

  -- civic status
  property_type               text,
  zoning_class                text,
  allowed_height_m            integer,
  current_average_height_m    integer,
  requested_scenario_height_m integer,
  height_band                 text,
  hal_clearance_status        text,
  civic_pressure_score        smallint,
  campaign_readiness          text,
  primary_concerns            text[] not null default '{}',
  recommended_next_action     text,
  dump_records_linked         integer,

  -- civic reports. has_reports false means "absent from the report dataset",
  -- which is NOT the same as a ward with zero reports.
  has_reports                 boolean not null default false,
  report_total                integer,
  report_unresolved           integer,

  -- representation (mp/mla come from the 243-ward list and cover ~1/3 of wards)
  representative              text,
  mp_name                     text,
  mla_name                    text,
  reservation                 text,
  legacy_ward_no              smallint,

  centroid                    geography(Point, 4326),
  geom                        geometry(MultiPolygon, 4326) not null,

  -- derived, kept in sync by the database rather than the loader
  pop_density numeric generated always as (
    case when area_sq_km > 0 then round(population / area_sq_km, 1) end
  ) stored,
  report_density numeric generated always as (
    case when area_sq_km > 0 then round(report_total / area_sq_km, 1) end
  ) stored,
  reports_per_1000 numeric generated always as (
    case when population > 0 then round(report_total::numeric / population * 1000, 2) end
  ) stored,

  constraint wards_corp_ward_uniq unique (corporation_id, ward_id)
);

comment on column public.wards.has_reports is
  'false = ward absent from the report dataset; distinct from zero reports';
comment on column public.wards.area_sq_km is
  'computed from the polygon — the GBA source layer has population but no area';


-- ---------------------------------------------------------------------------
-- ward_status_counts — severity histogram per ward (only the 4 expanded wards)
-- ---------------------------------------------------------------------------
create table public.ward_status_counts (
  ward_key     text not null references public.wards(ward_key) on delete cascade,
  status       text not null check (status in ('Critical','Severe','Moderate','Minor','Resolved')),
  report_count integer not null,
  primary key (ward_key, status)
);


-- ---------------------------------------------------------------------------
-- ward_reports — itemised reports. Only 4 wards carry these (491 rows);
-- the other 347 have headline counts on wards.report_total only.
-- Locations are free text with no coordinates, so these stay ward-level.
-- ---------------------------------------------------------------------------
create table public.ward_reports (
  id           bigint generated always as identity primary key,
  ward_key     text not null references public.wards(ward_key) on delete cascade,
  report_index integer,
  report_count integer,
  location     text not null,
  age_label    text,
  age_days_ago integer,
  status       text check (status in ('Critical','Severe','Moderate','Minor','Resolved')),
  constraint ward_reports_uniq unique (ward_key, report_index)
);


-- ---------------------------------------------------------------------------
-- metro
-- ---------------------------------------------------------------------------
create table public.metro_lines (
  name   text primary key,     -- 'Purple', 'Green', … the name IS the colour
  color  text not null,
  status text,
  -- jsonb sorts object keys, so the legend order has to be carried as data
  sort_order smallint not null default 1
);

create table public.stations (
  id           text primary key,
  name         text not null,
  status       text not null,
  -- Opened, or expected opening. Month precision ('2027-03') for the 43
  -- under-construction stations — a future opening has no known day — and
  -- full 'YYYY-MM-DD' for the ones already running. Kept as the source text
  -- so nothing is fabricated; service_date_exact below is the real date
  -- wherever there is one, for sorting and range queries.
  service_date text,
  -- Filled by the importer via _date(), not a generated column: to_date() is
  -- not immutable enough for one, and a generated column cannot be tolerant —
  -- it would raise on a malformed value instead of yielding NULL.
  service_date_exact date,
  interchange  boolean not null default false,
  note         text,
  ward_key     text references public.wards(ward_key) on delete set null,
  lat          double precision not null,
  lng          double precision not null,
  geom         geography(Point, 4326) not null
);

comment on column public.stations.ward_key is
  'null for the 12 stations outside the ward layer (Electronic City, Bommasandra, airport, Madavara)';

create table public.station_lines (
  station_id text not null references public.stations(id) on delete cascade,
  line_name  text not null references public.metro_lines(name) on delete cascade,
  -- Source order, preserved. The map colours an interchange by its first line,
  -- so sorting alphabetically here would silently recolour 6 of the 7
  -- multi-line stations (Majestic would go Purple -> Green).
  line_order smallint not null default 1,
  -- Position along the route, used to draw the line as a connected polyline.
  -- Distinct from line_order: that is where this line sits in the station's
  -- own list, this is where the station sits along this line.
  sequence smallint,
  primary key (station_id, line_name)
);


-- ---------------------------------------------------------------------------
-- road infrastructure — 8 point projects, 8 corridors
-- ---------------------------------------------------------------------------
create table public.infra_projects (
  id                  text primary key,
  name                text not null,
  type                text not null,
  authority           text,
  status              text,
  length_km           numeric(8,2),
  cost_crore          numeric(12,2),
  expected_completion text,
  description         text,
  ward_key            text references public.wards(ward_key) on delete set null,
  geom                geometry(Geometry, 4326) not null,
  constraint infra_projects_geom_type
    check (geometrytype(geom) in ('POINT','LINESTRING'))
);


-- ---------------------------------------------------------------------------
-- news — one row per unique URL, linked to any number of targets
-- ---------------------------------------------------------------------------
create table public.news_items (
  id           bigint generated always as identity primary key,
  url          text not null unique,
  title        text not null,
  source       text,
  published_on date
);

create table public.news_links (
  id          bigint generated always as identity primary key,
  news_id     bigint not null references public.news_items(id) on delete cascade,
  target_type text not null check (target_type in ('station','project','line','authority','general')),
  target_id   text          -- station/project/line/authority id; null for 'general'
);

-- target_id is nullable, so uniqueness needs an expression index rather than a PK
create unique index news_links_uniq
  on public.news_links (news_id, target_type, coalesce(target_id, ''));


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
create index wards_geom_gix         on public.wards using gist (geom);
create index wards_centroid_gix     on public.wards using gist (centroid);
create index wards_corporation_idx  on public.wards (corporation);
create index wards_report_total_idx on public.wards (report_total desc nulls last);
create index wards_pressure_idx     on public.wards (civic_pressure_score desc nulls last);
create index wards_name_idx         on public.wards (lower(ward_name));

create index ward_reports_ward_idx  on public.ward_reports (ward_key);
create index stations_geom_gix      on public.stations using gist (geom);
create index stations_ward_idx      on public.stations (ward_key);
create index infra_geom_gix         on public.infra_projects using gist (geom);
create index infra_ward_idx         on public.infra_projects (ward_key);
create index news_links_target_idx  on public.news_links (target_type, target_id);


-- ---------------------------------------------------------------------------
-- A geometry-free ward view — the table view and any listing should read this
-- rather than dragging 2.4 MB of polygons over the wire.
-- ---------------------------------------------------------------------------
create view public.ward_summary as
  select ward_key, corporation_id, ward_id, corporation, ward_name, ward_name_kn,
         zone_name, assembly, population, area_sq_km, pop_density,
         has_reports, report_total, report_unresolved, report_density, reports_per_1000,
         civic_pressure_score, campaign_readiness, property_type, zoning_class,
         height_band, allowed_height_m, current_average_height_m,
         representative, mp_name, mla_name, reservation,
         st_y(centroid::geometry) as lat,
         st_x(centroid::geometry) as lng
  from public.wards;


-- ---------------------------------------------------------------------------
-- Row level security: everything is public-read, writes need the service key
-- (the service role bypasses RLS entirely).
-- ---------------------------------------------------------------------------
alter table public.wards              enable row level security;
alter table public.ward_status_counts enable row level security;
alter table public.ward_reports       enable row level security;
alter table public.metro_lines        enable row level security;
alter table public.stations           enable row level security;
alter table public.station_lines      enable row level security;
alter table public.infra_projects     enable row level security;
alter table public.news_items         enable row level security;
alter table public.news_links         enable row level security;

create policy "public read" on public.wards              for select using (true);
create policy "public read" on public.ward_status_counts for select using (true);
create policy "public read" on public.ward_reports       for select using (true);
create policy "public read" on public.metro_lines        for select using (true);
create policy "public read" on public.stations           for select using (true);
create policy "public read" on public.station_lines      for select using (true);
create policy "public read" on public.infra_projects     for select using (true);
create policy "public read" on public.news_items         for select using (true);
create policy "public read" on public.news_links         for select using (true);

grant usage on schema public to anon, authenticated;
grant select on public.wards, public.ward_status_counts, public.ward_reports,
                public.metro_lines, public.stations, public.station_lines,
                public.infra_projects, public.news_items, public.news_links,
                public.ward_summary
  to anon, authenticated;


-- ============================================================================
-- Read API — these reproduce the exact shape of wards.geojson and points.json,
-- in camelCase, so the frontend swaps a fetch() for an RPC call and nothing
-- else changes.
-- ============================================================================

create or replace function public.wards_geojson(
  p_tolerance       double precision default 0,
  p_include_reports boolean          default true
)
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'type', 'FeatureCollection',
    'features', coalesce(jsonb_agg(
      jsonb_build_object(
        'type', 'Feature',
        'geometry', st_asgeojson(
          case when p_tolerance > 0
               then st_simplifypreservetopology(w.geom, p_tolerance)
               else w.geom end
        )::jsonb,
        'properties', jsonb_build_object(
          'wardKey',                  w.ward_key,
          'wardId',                   w.ward_id,
          'wardName',                 w.ward_name,
          'wardNameKn',               w.ward_name_kn,
          'corporation',              w.corporation,
          'corporationId',            w.corporation_id,
          'zoneName',                 w.zone_name,
          'assembly',                 w.assembly,
          'roDivision',               w.ro_division,
          'aroSubDivision',           w.aro_sub_division,
          'population',               w.population,
          'popMale',                  w.pop_male,
          'popFemale',                w.pop_female,
          'popSC',                    w.pop_sc,
          'popST',                    w.pop_st,
          'areaSqKm',                 w.area_sq_km,
          'popDensity',               w.pop_density,
          'centroid', jsonb_build_array(
                        st_x(w.centroid::geometry), st_y(w.centroid::geometry)),
          'propertyType',             w.property_type,
          'zoningClass',              w.zoning_class,
          'allowedHeightM',           w.allowed_height_m,
          'currentAverageHeightM',    w.current_average_height_m,
          'requestedScenarioHeightM', w.requested_scenario_height_m,
          'heightBand',               w.height_band,
          'halClearanceStatus',       w.hal_clearance_status,
          'civicPressureScore',       w.civic_pressure_score,
          'campaignReadiness',        w.campaign_readiness,
          'primaryConcerns',          to_jsonb(w.primary_concerns),
          'recommendedNextAction',    w.recommended_next_action,
          'dumpRecordsLinked',        w.dump_records_linked,
          'hasReports',               w.has_reports,
          'reportTotal',              w.report_total,
          'reportUnresolved',         w.report_unresolved,
          'reportDensity',            w.report_density,
          'reportsPer1000',           w.reports_per_1000,
          'representative',           w.representative,
          'mpName',                   w.mp_name,
          'mlaName',                  w.mla_name,
          'reservation',              w.reservation,
          'legacyWardNo',             w.legacy_ward_no,
          'statusCounts', case when p_include_reports then coalesce(
            (select jsonb_object_agg(sc.status, sc.report_count)
               from public.ward_status_counts sc where sc.ward_key = w.ward_key),
            '{}'::jsonb) else '{}'::jsonb end,
          'reports', case when p_include_reports then coalesce(
            (select jsonb_agg(jsonb_build_object(
                      'report_index', r.report_index,
                      'count',        r.report_count,
                      'location',     r.location,
                      'age',          r.age_label,
                      'age_days_ago', r.age_days_ago,
                      'status',       r.status)
                    order by r.report_index)
               from public.ward_reports r where r.ward_key = w.ward_key),
            '[]'::jsonb) else '[]'::jsonb end
        )
      )
      order by w.ward_key
    ), '[]'::jsonb)
  )
  from public.wards w;
$$;


-- helper: the news attached to one target, newest first.
-- Defined before points_json(), which calls it — Postgres validates a SQL
-- function body at CREATE time, so the callee must already exist.
create or replace function public._news_for(p_type text, p_id text)
returns jsonb
language sql
stable
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'title',  n.title,
           'url',    n.url,
           'date',   n.published_on,
           'source', n.source
         ) order by n.published_on desc nulls last, n.id), '[]'::jsonb)
  from public.news_links k
  join public.news_items n on n.id = k.news_id
  where k.target_type = p_type
    and ((p_id is null and k.target_id is null) or k.target_id = p_id);
$$;


create or replace function public.points_json()
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'metroLines', coalesce((
      select jsonb_object_agg(l.name, jsonb_build_object(
               'color', l.color, 'status', l.status, 'order', l.sort_order,
               'stations', coalesce((select jsonb_agg(sl.station_id
                                              order by sl.sequence nulls last, sl.station_id)
                                       from public.station_lines sl
                                      where sl.line_name = l.name), '[]'::jsonb)))
      from public.metro_lines l), '{}'::jsonb),

    'stations', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id',          s.id,
        'name',        s.name,
        'lat',         s.lat,
        'lng',         s.lng,
        'lines',       coalesce((select jsonb_agg(sl.line_name
                                          order by sl.line_order, sl.line_name)
                                   from public.station_lines sl where sl.station_id = s.id),
                                '[]'::jsonb),
        'status',      s.status,
        'date',        s.service_date,
        'interchange', s.interchange,
        'note',        coalesce(s.note, ''),
        'wardKey',     s.ward_key,
        'news',        public._news_for('station', s.id)
      ) order by s.id)
      from public.stations s), '[]'::jsonb),

    'infraLegend', '{}'::jsonb,

    'projects', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id',                  p.id,
        'name',                p.name,
        'type',                p.type,
        'authority',           p.authority,
        'status',              p.status,
        'lengthKm',            p.length_km,
        'costCrore',           p.cost_crore,
        'expectedCompletion',  p.expected_completion,
        'description',         p.description,
        -- geometry back out in the [lat, lng] order the frontend uses
        'point', case when geometrytype(p.geom) = 'POINT'
                      then jsonb_build_array(st_y(p.geom), st_x(p.geom)) end,
        -- st_dumppoints returns geometry_dump(path int[], geom geometry)
        'path',  case when geometrytype(p.geom) = 'LINESTRING' then (
                   select jsonb_agg(jsonb_build_array(st_y(d.geom), st_x(d.geom)) order by d.path)
                   from st_dumppoints(p.geom) d) end,
        'wardKey',             p.ward_key,
        'news',                public._news_for('project', p.id)
      ) order by p.id)
      from public.infra_projects p), '[]'::jsonb),

    'lineNews', coalesce((
      select jsonb_object_agg(l.name, public._news_for('line', l.name))
      from public.metro_lines l), '{}'::jsonb),

    'roadNewsGeneral', public._news_for('general', null::text),

    'roadNewsAuthorities', coalesce((
      select jsonb_object_agg(t.target_id, public._news_for('authority', t.target_id))
      from (select distinct target_id from public.news_links
             where target_type = 'authority' and target_id is not null) t), '{}'::jsonb)
  );
$$;


grant execute on function public.wards_geojson(double precision, boolean) to anon, authenticated;
grant execute on function public.points_json()                            to anon, authenticated;
grant execute on function public._news_for(text, text)                    to anon, authenticated;




-- ============================================================================
-- Write API — full reload from the two generated files. Service key only.
--
-- Design: TOLERANT ABOUT SHAPE, STRICT ABOUT MEANING.
--
-- Scraped civic data reliably arrives with month-precision dates, blank
-- numbers and the odd duplicate id, and it will do so again every time the
-- parsers re-run. So every scalar is coerced through a helper that yields
-- NULL rather than raising: one malformed field costs that field, not the
-- whole 369-row import.
--
-- What stays strict is anything load-bearing for meaning — a ward with
-- unparseable geometry cannot be drawn, and a row with no key cannot be
-- joined. Those rows are SKIPPED and COUNTED, never silently accepted: each
-- import returns a report of what went in and what did not, and
-- verify_ward_atlas.sql compares those totals against the expected numbers.
-- Tolerance is at the edge; the tripwire is right behind it.
-- ============================================================================

create or replace function public._int(t text) returns integer
language plpgsql immutable as $fn$
begin return t::integer; exception when others then return null; end $fn$;

create or replace function public._num(t text) returns numeric
language plpgsql immutable as $fn$
begin return t::numeric; exception when others then return null; end $fn$;

create or replace function public._bool(t text) returns boolean
language plpgsql immutable as $fn$
begin return coalesce(t::boolean, false); exception when others then return false; end $fn$;

-- Only a full YYYY-MM-DD is a date. Month-precision values ('2027-03', the
-- expected opening of an unbuilt station) deliberately yield NULL here; the
-- source string is kept verbatim in its own text column.
create or replace function public._date(t text) returns date
language plpgsql immutable as $fn$
begin
  return case when t ~ '^\d{4}-\d{2}-\d{2}$' then to_date(t, 'YYYY-MM-DD') end;
exception when others then return null; end $fn$;

create or replace function public._geom(g jsonb) returns geometry
language plpgsql immutable as $fn$
begin return st_setsrid(st_geomfromgeojson(g), 4326);
exception when others then return null; end $fn$;


create or replace function public.import_wards(fc jsonb)
returns jsonb
language plpgsql
as $fn$
declare
  v_in      integer;
  v_wards   integer;
  v_status  integer;
  v_reports integer;
begin
  v_in := jsonb_array_length(coalesce(fc->'features', '[]'::jsonb));

  insert into public.wards (
    ward_key, corporation_id, ward_id, corporation, ward_name, ward_name_kn,
    zone_name, assembly, ro_division, aro_sub_division,
    population, pop_male, pop_female, pop_sc, pop_st, area_sq_km,
    property_type, zoning_class, allowed_height_m, current_average_height_m,
    requested_scenario_height_m, height_band, hal_clearance_status,
    civic_pressure_score, campaign_readiness, primary_concerns,
    recommended_next_action, dump_records_linked,
    has_reports, report_total, report_unresolved,
    representative, mp_name, mla_name, reservation, legacy_ward_no,
    centroid, geom
  )
  select distinct on (x.p->>'wardKey')
    x.p->>'wardKey',
    public._int(x.p->>'corporationId')::smallint,
    public._int(x.p->>'wardId')::smallint,
    x.p->>'corporation',
    x.p->>'wardName',
    x.p->>'wardNameKn',
    x.p->>'zoneName',
    x.p->>'assembly',
    x.p->>'roDivision',
    x.p->>'aroSubDivision',
    public._int(x.p->>'population'),
    public._int(x.p->>'popMale'),
    public._int(x.p->>'popFemale'),
    public._int(x.p->>'popSC'),
    public._int(x.p->>'popST'),
    public._num(x.p->>'areaSqKm'),
    x.p->>'propertyType',
    x.p->>'zoningClass',
    public._int(x.p->>'allowedHeightM'),
    public._int(x.p->>'currentAverageHeightM'),
    public._int(x.p->>'requestedScenarioHeightM'),
    x.p->>'heightBand',
    x.p->>'halClearanceStatus',
    public._int(x.p->>'civicPressureScore')::smallint,
    x.p->>'campaignReadiness',
    coalesce((select array_agg(c #>> '{}')
                from jsonb_array_elements(
                       case when jsonb_typeof(x.p->'primaryConcerns') = 'array'
                            then x.p->'primaryConcerns' else '[]'::jsonb end) c),
             '{}'::text[]),
    x.p->>'recommendedNextAction',
    public._int(x.p->>'dumpRecordsLinked'),
    public._bool(x.p->>'hasReports'),
    public._int(x.p->>'reportTotal'),
    public._int(x.p->>'reportUnresolved'),
    x.p->>'representative',
    x.p->>'mpName',
    x.p->>'mlaName',
    x.p->>'reservation',
    public._int(x.p->>'legacyWardNo')::smallint,
    case when jsonb_typeof(x.p->'centroid') = 'array'
         then st_setsrid(st_point(public._num(x.p->'centroid'->>0)::double precision,
                                  public._num(x.p->'centroid'->>1)::double precision),
                         4326)::geography end,
    st_multi(x.g)
  from jsonb_array_elements(coalesce(fc->'features', '[]'::jsonb)) f
  cross join lateral (select f->'properties'             as p,
                             public._geom(f->'geometry') as g) x
  -- strict: no key, no ids or no geometry means the row cannot be joined or drawn
  where coalesce(x.p->>'wardKey', '') <> ''
    and x.g is not null
    and public._int(x.p->>'corporationId') is not null
    and public._int(x.p->>'wardId') is not null
  order by x.p->>'wardKey'
  on conflict (ward_key) do update set
    corporation_id = excluded.corporation_id,
    ward_id        = excluded.ward_id,
    corporation    = excluded.corporation,
    ward_name      = excluded.ward_name,
    ward_name_kn   = excluded.ward_name_kn,
    zone_name      = excluded.zone_name,
    assembly       = excluded.assembly,
    ro_division    = excluded.ro_division,
    aro_sub_division = excluded.aro_sub_division,
    population     = excluded.population,
    pop_male       = excluded.pop_male,
    pop_female     = excluded.pop_female,
    pop_sc         = excluded.pop_sc,
    pop_st         = excluded.pop_st,
    area_sq_km     = excluded.area_sq_km,
    property_type  = excluded.property_type,
    zoning_class   = excluded.zoning_class,
    allowed_height_m = excluded.allowed_height_m,
    current_average_height_m = excluded.current_average_height_m,
    requested_scenario_height_m = excluded.requested_scenario_height_m,
    height_band    = excluded.height_band,
    hal_clearance_status = excluded.hal_clearance_status,
    civic_pressure_score = excluded.civic_pressure_score,
    campaign_readiness   = excluded.campaign_readiness,
    primary_concerns     = excluded.primary_concerns,
    recommended_next_action = excluded.recommended_next_action,
    dump_records_linked  = excluded.dump_records_linked,
    has_reports    = excluded.has_reports,
    report_total   = excluded.report_total,
    report_unresolved = excluded.report_unresolved,
    representative = excluded.representative,
    mp_name        = excluded.mp_name,
    mla_name       = excluded.mla_name,
    reservation    = excluded.reservation,
    legacy_ward_no = excluded.legacy_ward_no,
    centroid       = excluded.centroid,
    geom           = excluded.geom;

  get diagnostics v_wards = row_count;

  -- Children are replaced, not merged. Scoped to the wards in this payload:
  -- a bare DELETE is rejected by Supabase's pg_safeupdate guard, and scoping
  -- means a partial payload no longer wipes wards it does not mention.
  delete from public.ward_status_counts
   where ward_key in (select f->'properties'->>'wardKey'
                        from jsonb_array_elements(coalesce(fc->'features','[]'::jsonb)) f);

  delete from public.ward_reports
   where ward_key in (select f->'properties'->>'wardKey'
                        from jsonb_array_elements(coalesce(fc->'features','[]'::jsonb)) f);

  insert into public.ward_status_counts (ward_key, status, report_count)
  select f->'properties'->>'wardKey', e.key, public._int(e.value)
  from jsonb_array_elements(coalesce(fc->'features','[]'::jsonb)) f
  cross join lateral jsonb_each_text(
    case when jsonb_typeof(f->'properties'->'statusCounts') = 'object'
         then f->'properties'->'statusCounts' else '{}'::jsonb end) e
  where e.key in ('Critical','Severe','Moderate','Minor','Resolved')
    and public._int(e.value) is not null
    and exists (select 1 from public.wards w
                 where w.ward_key = f->'properties'->>'wardKey')
  on conflict (ward_key, status) do update set report_count = excluded.report_count;

  get diagnostics v_status = row_count;

  insert into public.ward_reports
    (ward_key, report_index, report_count, location, age_label, age_days_ago, status)
  select distinct on (f->'properties'->>'wardKey', public._int(r->>'report_index'))
         f->'properties'->>'wardKey',
         public._int(r->>'report_index'),
         public._int(r->>'count'),
         coalesce(nullif(r->>'location',''), '(no location given)'),
         r->>'age',
         public._int(r->>'age_days_ago'),
         -- anything outside the known set becomes NULL rather than failing the
         -- CHECK constraint and aborting the whole load
         case when r->>'status' in ('Critical','Severe','Moderate','Minor','Resolved')
              then r->>'status' end
  from jsonb_array_elements(coalesce(fc->'features','[]'::jsonb)) f
  cross join lateral jsonb_array_elements(
    case when jsonb_typeof(f->'properties'->'reports') = 'array'
         then f->'properties'->'reports' else '[]'::jsonb end) r
  where exists (select 1 from public.wards w
                 where w.ward_key = f->'properties'->>'wardKey')
  order by f->'properties'->>'wardKey', public._int(r->>'report_index')
  on conflict (ward_key, report_index) do nothing;

  get diagnostics v_reports = row_count;

  return jsonb_build_object(
    'features_in_payload', v_in,
    'wards_upserted',      v_wards,
    'wards_skipped',       v_in - v_wards,
    'status_counts',       v_status,
    'itemised_reports',    v_reports
  );
end;
$fn$;


create or replace function public.import_points(doc jsonb)
returns jsonb
language plpgsql
as $fn$
declare
  v_st_in    integer;
  v_pr_in    integer;
  v_lines    integer;
  v_stations integer;
  v_stlines  integer;
  v_projects integer;
  v_news     integer;
  v_links    integer;
begin
  v_st_in := jsonb_array_length(coalesce(doc->'stations','[]'::jsonb));
  v_pr_in := jsonb_array_length(coalesce(doc->'projects','[]'::jsonb));

  -- metro lines
  insert into public.metro_lines (name, color, status, sort_order)
  select e.key,
         coalesce(nullif(e.value->>'color',''), '#888888'),
         e.value->>'status',
         coalesce(public._int(e.value->>'order'), 1)::smallint
  from jsonb_each(case when jsonb_typeof(doc->'metroLines') = 'object'
                       then doc->'metroLines' else '{}'::jsonb end) e
  on conflict (name) do update
    set color = excluded.color, status = excluded.status,
        sort_order = excluded.sort_order;
  get diagnostics v_lines = row_count;

  -- Stations. distinct on: interchanges are sometimes listed once per line in
  -- the source, and ON CONFLICT DO UPDATE cannot touch one row twice in a
  -- single command. Their lines are gathered separately into station_lines.
  insert into public.stations
    (id, name, status, service_date, service_date_exact,
     interchange, note, ward_key, lat, lng, geom)
  select distinct on (s->>'id')
         s->>'id',
         coalesce(nullif(s->>'name',''), s->>'id'),
         coalesce(nullif(s->>'status',''), 'unknown'),
         nullif(s->>'date',''),          -- verbatim: may be month-precision
         public._date(s->>'date'),       -- a real date only when it is one
         public._bool(s->>'interchange'),
         nullif(s->>'note',''),
         nullif(s->>'wardKey',''),
         public._num(s->>'lat')::double precision,
         public._num(s->>'lng')::double precision,
         st_setsrid(st_point(public._num(s->>'lng')::double precision,
                             public._num(s->>'lat')::double precision), 4326)::geography
  from jsonb_array_elements(coalesce(doc->'stations','[]'::jsonb)) s
  -- strict: no id or no usable position means it is not a map point
  where coalesce(s->>'id','') <> ''
    and public._num(s->>'lat') is not null
    and public._num(s->>'lng') is not null
  order by s->>'id'
  on conflict (id) do update set
    name = excluded.name, status = excluded.status,
    service_date = excluded.service_date,
    service_date_exact = excluded.service_date_exact,
    interchange = excluded.interchange,
    note = excluded.note, ward_key = excluded.ward_key,
    lat = excluded.lat, lng = excluded.lng, geom = excluded.geom;
  get diagnostics v_stations = row_count;

  -- a ward_key that does not exist would violate the FK; null it instead
  update public.stations s set ward_key = null
   where s.ward_key is not null
     and not exists (select 1 from public.wards w where w.ward_key = s.ward_key);

  delete from public.station_lines
   where station_id in (select s->>'id'
                          from jsonb_array_elements(coalesce(doc->'stations','[]'::jsonb)) s);

  insert into public.station_lines (station_id, line_name, line_order)
  select distinct on (s->>'id', l #>> '{}')
         s->>'id', l #>> '{}', t.ord::smallint
  from jsonb_array_elements(coalesce(doc->'stations','[]'::jsonb)) s
  cross join lateral jsonb_array_elements(
    case when jsonb_typeof(s->'lines') = 'array' then s->'lines' else '[]'::jsonb end)
    with ordinality as t(l, ord)
  where exists (select 1 from public.stations st where st.id = s->>'id')
    and exists (select 1 from public.metro_lines m where m.name = l #>> '{}')
  order by s->>'id', l #>> '{}', t.ord
  on conflict (station_id, line_name) do update set line_order = excluded.line_order;
  get diagnostics v_stlines = row_count;

  -- running order along each route, from metroLines[<line>].stations
  update public.station_lines sl
     set sequence = src.seq
    from (select e.key as line_name, t.sid #>> '{}' as station_id, t.ord::smallint as seq
            from jsonb_each(case when jsonb_typeof(doc->'metroLines') = 'object'
                                 then doc->'metroLines' else '{}'::jsonb end) e
            cross join lateral jsonb_array_elements(
              case when jsonb_typeof(e.value->'stations') = 'array'
                   then e.value->'stations' else '[]'::jsonb end)
              with ordinality as t(sid, ord)) src
   where sl.line_name = src.line_name and sl.station_id = src.station_id;

  -- road projects: a point or a path, both arriving as [lat, lng]
  insert into public.infra_projects
    (id, name, type, authority, status, length_km, cost_crore,
     expected_completion, description, ward_key, geom)
  select distinct on (p->>'id')
         p->>'id',
         coalesce(nullif(p->>'name',''), p->>'id'),
         coalesce(nullif(p->>'type',''), 'unknown'),
         p->>'authority',
         p->>'status',
         public._num(p->>'lengthKm'),
         public._num(p->>'costCrore'),
         p->>'expectedCompletion',
         p->>'description',
         nullif(p->>'wardKey',''),
         case
           when jsonb_typeof(p->'point') = 'array'
             then st_setsrid(st_point(public._num(p->'point'->>1)::double precision,
                                      public._num(p->'point'->>0)::double precision), 4326)
           else (
             select st_setsrid(st_makeline(
                      array_agg(st_point(public._num(c->>1)::double precision,
                                         public._num(c->>0)::double precision) order by ord)), 4326)
             from jsonb_array_elements(
                    case when jsonb_typeof(p->'path') = 'array'
                         then p->'path' else '[]'::jsonb end) with ordinality as t(c, ord)
           )
         end
  from jsonb_array_elements(coalesce(doc->'projects','[]'::jsonb)) p
  -- strict: needs an id, and either a point or a path of at least two vertices
  where coalesce(p->>'id','') <> ''
    and (jsonb_typeof(p->'point') = 'array'
         or (jsonb_typeof(p->'path') = 'array'
             and jsonb_array_length(p->'path') >= 2))
  order by p->>'id'
  on conflict (id) do update set
    name = excluded.name, type = excluded.type, authority = excluded.authority,
    status = excluded.status, length_km = excluded.length_km,
    cost_crore = excluded.cost_crore,
    expected_completion = excluded.expected_completion,
    description = excluded.description, ward_key = excluded.ward_key,
    geom = excluded.geom;
  get diagnostics v_projects = row_count;

  update public.infra_projects p set ward_key = null
   where p.ward_key is not null
     and not exists (select 1 from public.wards w where w.ward_key = p.ward_key);

  -- News. Collected once into a temp table so items and links can be counted
  -- separately; the same article legitimately appears under several targets.
  create temp table _news_src on commit drop as
    select 'station'::text as target_type, s->>'id' as target_id, nw as item
      from jsonb_array_elements(coalesce(doc->'stations','[]'::jsonb)) s
      cross join lateral jsonb_array_elements(
        case when jsonb_typeof(s->'news') = 'array' then s->'news' else '[]'::jsonb end) nw
    union all
    select 'project', p->>'id', nw
      from jsonb_array_elements(coalesce(doc->'projects','[]'::jsonb)) p
      cross join lateral jsonb_array_elements(
        case when jsonb_typeof(p->'news') = 'array' then p->'news' else '[]'::jsonb end) nw
    union all
    select 'line', e.key, nw
      from jsonb_each(case when jsonb_typeof(doc->'lineNews') = 'object'
                           then doc->'lineNews' else '{}'::jsonb end) e
      cross join lateral jsonb_array_elements(
        case when jsonb_typeof(e.value) = 'array' then e.value else '[]'::jsonb end) nw
    union all
    select 'authority', e.key, nw
      from jsonb_each(case when jsonb_typeof(doc->'roadNewsAuthorities') = 'object'
                           then doc->'roadNewsAuthorities' else '{}'::jsonb end) e
      cross join lateral jsonb_array_elements(
        case when jsonb_typeof(e.value) = 'array' then e.value else '[]'::jsonb end) nw
    union all
    select 'general', null, nw
      from jsonb_array_elements(
        case when jsonb_typeof(doc->'roadNewsGeneral') = 'array'
             then doc->'roadNewsGeneral' else '[]'::jsonb end) nw;

  insert into public.news_items (url, title, source, published_on)
  select distinct on (item->>'url')
         item->>'url',
         coalesce(nullif(item->>'title',''), '(untitled)'),
         nullif(item->>'source',''),
         public._date(item->>'date')
  from _news_src
  where coalesce(item->>'url','') <> ''
  order by item->>'url'
  on conflict (url) do update
    set title        = excluded.title,
        source       = coalesce(excluded.source, news_items.source),
        published_on = coalesce(excluded.published_on, news_items.published_on);
  get diagnostics v_news = row_count;

  insert into public.news_links (news_id, target_type, target_id)
  select distinct n.id, s.target_type, s.target_id
  from _news_src s
  join public.news_items n on n.url = s.item->>'url'
  on conflict do nothing;
  get diagnostics v_links = row_count;

  return jsonb_build_object(
    'stations_in_payload', v_st_in,
    'stations_upserted',   v_stations,
    'stations_skipped',    v_st_in - v_stations,
    'metro_lines',         v_lines,
    'station_lines',       v_stlines,
    'projects_in_payload', v_pr_in,
    'projects_upserted',   v_projects,
    'projects_skipped',    v_pr_in - v_projects,
    'news_items',          v_news,
    'news_links',          v_links
  );
end;
$fn$;

-- import functions and coercion helpers are service-key only
revoke all on function public.import_wards(jsonb)  from public, anon, authenticated;
revoke all on function public.import_points(jsonb) from public, anon, authenticated;
