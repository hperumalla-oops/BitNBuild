-- ============================================================================
-- Query API for the chat agent.
--
-- Additive and non-destructive: creates functions only, touches no tables and
-- drops nothing from 002. Safe to re-run.
--
-- PostgREST cannot express PostGIS predicates, so the spatial questions the
-- agent needs ("which ward is this point in", "stations within 2km") are
-- exposed as RPCs here. Everything else it can reach as a plain table query.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Which ward contains a point?
-- ---------------------------------------------------------------------------
create or replace function public.ward_at_point(p_lat double precision,
                                                p_lng double precision)
returns table (
  ward_key text, ward_name text, corporation text, assembly text,
  population integer, area_sq_km numeric, report_total integer,
  report_density numeric, civic_pressure_score smallint
)
language sql stable as $fn$
  select w.ward_key, w.ward_name, w.corporation, w.assembly,
         w.population, w.area_sq_km, w.report_total,
         w.report_density, w.civic_pressure_score
  from public.wards w
  where st_contains(w.geom, st_setsrid(st_point(p_lng, p_lat), 4326))
  limit 1;
$fn$;

-- ---------------------------------------------------------------------------
-- Metro stations within a radius, nearest first.
-- ---------------------------------------------------------------------------
create or replace function public.stations_near(p_lat double precision,
                                                p_lng double precision,
                                                p_radius_m double precision default 2000)
returns table (
  id text, name text, lines text[], status text, service_date text,
  interchange boolean, ward_key text, ward_name text, distance_m double precision
)
language sql stable as $fn$
  select s.id, s.name,
         coalesce(array(select sl.line_name from public.station_lines sl
                         where sl.station_id = s.id
                         order by sl.line_order, sl.line_name), '{}'::text[]),
         s.status, s.service_date, s.interchange, s.ward_key, w.ward_name,
         round(st_distance(s.geom, st_setsrid(st_point(p_lng, p_lat), 4326)::geography)::numeric, 1)::double precision
  from public.stations s
  left join public.wards w on w.ward_key = s.ward_key
  where st_dwithin(s.geom, st_setsrid(st_point(p_lng, p_lat), 4326)::geography, p_radius_m)
  order by s.geom <-> st_setsrid(st_point(p_lng, p_lat), 4326)::geography
  limit 25;
$fn$;

-- ---------------------------------------------------------------------------
-- Road projects whose geometry touches a ward.
-- ---------------------------------------------------------------------------
create or replace function public.projects_in_ward(p_ward_key text)
returns table (
  id text, name text, type text, authority text, status text,
  length_km numeric, cost_crore numeric, expected_completion text
)
language sql stable as $fn$
  select p.id, p.name, p.type, p.authority, p.status,
         p.length_km, p.cost_crore, p.expected_completion
  from public.infra_projects p
  join public.wards w on w.ward_key = p_ward_key
  where st_intersects(p.geom, w.geom)
  order by p.id;
$fn$;

-- ---------------------------------------------------------------------------
-- Fuzzy ward lookup by name — the agent gets names from users, not keys.
-- ---------------------------------------------------------------------------
create or replace function public.ward_search(p_query text, p_limit int default 8)
returns table (
  ward_key text, ward_name text, corporation text, assembly text,
  population integer, area_sq_km numeric, report_total integer,
  report_unresolved integer, report_density numeric, reports_per_1000 numeric,
  civic_pressure_score smallint, campaign_readiness text,
  property_type text, zoning_class text, height_band text,
  representative text, mla_name text, mp_name text
)
language sql stable as $fn$
  select w.ward_key, w.ward_name, w.corporation, w.assembly,
         w.population, w.area_sq_km, w.report_total,
         w.report_unresolved, w.report_density, w.reports_per_1000,
         w.civic_pressure_score, w.campaign_readiness,
         w.property_type, w.zoning_class, w.height_band,
         w.representative, w.mla_name, w.mp_name
  from public.wards w
  where lower(w.ward_name) like '%' || lower(trim(p_query)) || '%'
     or lower(w.zone_name)  like '%' || lower(trim(p_query)) || '%'
     or w.ward_key = trim(p_query)
  order by
    -- exact name first, then prefix, then anything containing it
    case when lower(w.ward_name) = lower(trim(p_query)) then 0
         when lower(w.ward_name) like lower(trim(p_query)) || '%' then 1
         else 2 end,
    w.report_total desc nulls last
  limit greatest(1, least(p_limit, 50));
$fn$;

-- ---------------------------------------------------------------------------
-- Ward rankings. One entry point for "worst N by X", optionally filtered.
-- The metric name is whitelisted, so no SQL is ever built from model output.
-- ---------------------------------------------------------------------------
create or replace function public.ward_rank(p_metric text,
                                            p_limit int default 10,
                                            p_corporation text default null,
                                            p_ascending boolean default false)
returns table (
  ward_key text, ward_name text, corporation text, metric text,
  value numeric, population integer, area_sq_km numeric, report_total integer
)
language sql stable as $fn$
  select w.ward_key, w.ward_name, w.corporation, p_metric,
         case p_metric
           when 'report_total'         then w.report_total::numeric
           when 'report_unresolved'    then w.report_unresolved::numeric
           when 'report_density'       then w.report_density
           when 'reports_per_1000'     then w.reports_per_1000
           when 'civic_pressure_score' then w.civic_pressure_score::numeric
           when 'population'           then w.population::numeric
           when 'pop_density'          then w.pop_density
           when 'area_sq_km'           then w.area_sq_km
           when 'allowed_height_m'     then w.allowed_height_m::numeric
         end,
         w.population, w.area_sq_km, w.report_total
  from public.wards w
  where (p_corporation is null or lower(w.corporation) = lower(p_corporation))
    and case p_metric
          when 'report_total'         then w.report_total
          when 'report_unresolved'    then w.report_unresolved
          when 'civic_pressure_score' then w.civic_pressure_score
          when 'population'           then w.population
          when 'allowed_height_m'     then w.allowed_height_m
          else 1 end is not null
    and case p_metric
          when 'report_density'   then w.report_density
          when 'reports_per_1000' then w.reports_per_1000
          when 'pop_density'      then w.pop_density
          when 'area_sq_km'       then w.area_sq_km
          else 1 end is not null
  order by
    case when p_ascending then
      case p_metric
        when 'report_total'         then w.report_total::numeric
        when 'report_unresolved'    then w.report_unresolved::numeric
        when 'report_density'       then w.report_density
        when 'reports_per_1000'     then w.reports_per_1000
        when 'civic_pressure_score' then w.civic_pressure_score::numeric
        when 'population'           then w.population::numeric
        when 'pop_density'          then w.pop_density
        when 'area_sq_km'           then w.area_sq_km
        when 'allowed_height_m'     then w.allowed_height_m::numeric
      end end asc nulls last,
    case when not p_ascending then
      case p_metric
        when 'report_total'         then w.report_total::numeric
        when 'report_unresolved'    then w.report_unresolved::numeric
        when 'report_density'       then w.report_density
        when 'reports_per_1000'     then w.reports_per_1000
        when 'civic_pressure_score' then w.civic_pressure_score::numeric
        when 'population'           then w.population::numeric
        when 'pop_density'          then w.pop_density
        when 'area_sq_km'           then w.area_sq_km
        when 'allowed_height_m'     then w.allowed_height_m::numeric
      end end desc nulls last
  limit greatest(1, least(p_limit, 50));
$fn$;

-- ---------------------------------------------------------------------------
-- The itemised reports for one ward (only 4 wards have any).
-- ---------------------------------------------------------------------------
create or replace function public.ward_reports_for(p_ward_key text)
returns table (
  report_index integer, report_count integer, location text,
  age_label text, age_days_ago integer, status text
)
language sql stable as $fn$
  select r.report_index, r.report_count, r.location,
         r.age_label, r.age_days_ago, r.status
  from public.ward_reports r
  where r.ward_key = p_ward_key
  order by r.report_count desc nulls last, r.report_index
  limit 100;
$fn$;

-- ---------------------------------------------------------------------------
-- Reads are safe for anon; the server uses the service key anyway.
-- ---------------------------------------------------------------------------
grant execute on function public.ward_at_point(double precision, double precision) to anon, authenticated;
grant execute on function public.stations_near(double precision, double precision, double precision) to anon, authenticated;
grant execute on function public.projects_in_ward(text) to anon, authenticated;
grant execute on function public.ward_search(text, int) to anon, authenticated;
grant execute on function public.ward_rank(text, int, text, boolean) to anon, authenticated;
grant execute on function public.ward_reports_for(text) to anon, authenticated;
