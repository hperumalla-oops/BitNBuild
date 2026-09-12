-- Bangalore Zoning Data Ingestion Engine
-- Run this once in the Supabase SQL editor before running ingest.py.
-- Safe to re-run: uses IF NOT EXISTS / CREATE OR REPLACE throughout.

create extension if not exists vector;

-- ============================================================
-- Tabular outputs (one table per FAR/setback/height/etc. lookup)
-- Column names mirror the row shapes documented in CLAUDE.md.
-- Every table carries source_id (which SOURCES entry produced the
-- row) so re-ingestion can delete-and-replace scoped to one source
-- without disturbing rows contributed by other sources.
-- ============================================================

create table if not exists permitted_uses (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    use_type text not null,
    use_category text,
    permission text not null check (permission in ('permitted', 'ancillary', 'no')),
    conditions text,
    source_table text,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_permitted_uses_source_id on permitted_uses (source_id);
create index if not exists idx_permitted_uses_zone on permitted_uses (zone);

create table if not exists far_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    ring text not null,
    road_width_min_m numeric,
    road_width_max_m numeric,
    far numeric not null,
    premium_far numeric,
    premium_conditions text,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_far_rules_source_id on far_rules (source_id);
create index if not exists idx_far_rules_zone_ring on far_rules (zone, ring);

create table if not exists setback_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    building_height_min_m numeric,
    building_height_max_m numeric,
    plot_area_min_sqm numeric,
    plot_area_max_sqm numeric,
    front_setback_m numeric,
    rear_setback_m numeric,
    side1_setback_m numeric,
    side2_setback_m numeric,
    source text,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_setback_rules_source_id on setback_rules (source_id);

create table if not exists height_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    ring text not null,
    road_width_min_m numeric,
    road_width_max_m numeric,
    max_height_m numeric,
    max_floors int,
    max_floors_description text,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_height_rules_source_id on height_rules (source_id);
create index if not exists idx_height_rules_zone_ring on height_rules (zone, ring);

create table if not exists parking_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    use_type text not null,
    unit text,
    ecs_required numeric,
    visitor_parking_pct numeric,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_parking_rules_source_id on parking_rules (source_id);

create table if not exists coverage_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    ring text not null,
    max_coverage_pct numeric,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_coverage_rules_source_id on coverage_rules (source_id);

create table if not exists plot_size_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    ring text not null,
    min_plot_size_sqm numeric,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_plot_size_rules_source_id on plot_size_rules (source_id);

create table if not exists density_rules (
    id bigint generated always as identity primary key,
    source_id text not null,
    is_synthetic boolean not null default false,
    zone text not null,
    ring text not null,
    max_dwelling_units_per_ha numeric,
    source_page int,
    created_at timestamptz not null default now()
);
create index if not exists idx_density_rules_source_id on density_rules (source_id);

-- Log of each table-extraction run, standing in for the old JSON
-- "metadata" header (source_section, description, extracted_at).
create table if not exists table_extraction_runs (
    id bigint generated always as identity primary key,
    table_name text not null,
    source_id text not null,
    is_synthetic boolean not null default false,
    source_section text,
    description text,
    row_count int not null default 0,
    extracted_at timestamptz not null default now()
);
create index if not exists idx_table_extraction_runs_table_source on table_extraction_runs (table_name, source_id);

-- ============================================================
-- RAG chunks — regulatory text chunks + OpenAI embeddings.
-- text-embedding-3-small produces 1536-dimensional vectors.
-- ============================================================

create table if not exists rag_chunks (
    id bigint generated always as identity primary key,
    chunk_id text not null unique,
    source_id text not null,
    is_synthetic boolean not null default false,
    source_url text,
    section text,
    clause_number text,
    clause_title text,
    text text not null,
    page_number int,
    char_offset_start int,
    char_offset_end int,
    embedding vector(1536),
    created_at timestamptz not null default now()
);
create index if not exists idx_rag_chunks_source_id on rag_chunks (source_id);
create index if not exists idx_rag_chunks_section on rag_chunks (section);

-- Approximate nearest-neighbor index for cosine-similarity retrieval.
-- ivfflat needs rows in the table to train on; if this errors on a
-- brand-new empty table, re-run it after the first ingest.
create index if not exists idx_rag_chunks_embedding_cosine
    on rag_chunks using ivfflat (embedding vector_cosine_ops)
    with (lists = 100);

-- Convenience RPC for semantic search from application code:
-- select * from match_rag_chunks('[0.01, ...]'::vector, 0.75, 8);
create or replace function match_rag_chunks(
    query_embedding vector(1536),
    match_threshold float default 0.75,
    match_count int default 8
)
returns table (
    chunk_id text,
    source_id text,
    section text,
    clause_number text,
    clause_title text,
    text text,
    page_number int,
    similarity float
)
language sql stable
as $$
    select
        rag_chunks.chunk_id,
        rag_chunks.source_id,
        rag_chunks.section,
        rag_chunks.clause_number,
        rag_chunks.clause_title,
        rag_chunks.text,
        rag_chunks.page_number,
        1 - (rag_chunks.embedding <=> query_embedding) as similarity
    from rag_chunks
    where 1 - (rag_chunks.embedding <=> query_embedding) > match_threshold
    order by rag_chunks.embedding <=> query_embedding
    limit match_count;
$$;

-- ============================================================
-- Source registry + PDF deep-linking.
-- Mirrors ingest.py's SOURCES list so any row/chunk's (source_id, page)
-- can be turned into a clickable link straight to that page of the
-- original PDF — "https://.../doc.pdf#page=N" is honored natively by
-- every major browser's built-in PDF viewer, no extra hosting needed.
-- ============================================================

create table if not exists sources (
    source_id text primary key,
    url text not null,
    filename text,
    type text,
    description text
);

create or replace function pdf_link(p_source_id text, p_page int)
returns text
language sql stable
as $$
    select case
        when p_page is null then null
        else (select url from sources where source_id = p_source_id) || '#page=' || p_page
    end;
$$;

-- Usage:
--   select *, pdf_link(source_id, source_page) as pdf_url from far_rules;
--   select *, pdf_link(source_id, page_number) as pdf_url from rag_chunks;
