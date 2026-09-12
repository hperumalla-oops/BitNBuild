"""Supabase (supabase-py) client and write helpers.

All writes are scoped delete-then-insert: for a given (table, source_id)
pair, existing rows from that source are deleted before the freshly
extracted rows are inserted. This makes re-running the pipeline for a
single source (`--only <source_id>`) or the whole registry idempotent
without disturbing rows contributed by other sources to the same table.
"""

import os

from supabase import Client, create_client

_client: Client | None = None

RAG_TABLE = "rag_chunks"
RUNS_TABLE = "table_extraction_runs"
SOURCES_TABLE = "sources"


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SECRET_KEY"]
        _client = create_client(url, key)
    return _client


def replace_table_rows(
    table_name: str, source_id: str, rows: list[dict], is_synthetic: bool = False
) -> int:
    """Delete existing rows for source_id in table_name, then insert rows."""
    client = get_client()
    client.table(table_name).delete().eq("source_id", source_id).execute()
    if rows:
        for row in rows:
            row["source_id"] = source_id
            row["is_synthetic"] = is_synthetic
        # Supabase/PostgREST batches inserts in chunks to stay under request size limits.
        for i in range(0, len(rows), 500):
            client.table(table_name).insert(rows[i : i + 500]).execute()
    return len(rows)


def log_extraction_run(
    table_name: str,
    source_id: str,
    source_section: str | None,
    description: str | None,
    row_count: int,
    is_synthetic: bool = False,
) -> None:
    client = get_client()
    client.table(RUNS_TABLE).insert(
        {
            "table_name": table_name,
            "source_id": source_id,
            "source_section": source_section,
            "description": description,
            "row_count": row_count,
            "is_synthetic": is_synthetic,
        }
    ).execute()


def upsert_sources(sources: list[dict]) -> None:
    """Upsert the SOURCES registry into the `sources` lookup table, so
    pdf_link(source_id, page) can resolve a URL for any row/chunk."""
    client = get_client()
    rows = [
        {
            "source_id": s["id"],
            "url": s["url"],
            "filename": s.get("filename"),
            "type": s["type"],
            "description": s.get("description"),
        }
        for s in sources
    ]
    client.table(SOURCES_TABLE).upsert(rows).execute()


def replace_rag_chunks(source_id: str, chunks: list[dict], is_synthetic: bool = False) -> int:
    """Delete existing RAG chunks for source_id, then insert fresh ones (with embeddings)."""
    client = get_client()
    client.table(RAG_TABLE).delete().eq("source_id", source_id).execute()
    if chunks:
        for chunk in chunks:
            chunk["is_synthetic"] = is_synthetic
        for i in range(0, len(chunks), 200):
            client.table(RAG_TABLE).insert(chunks[i : i + 200]).execute()
    return len(chunks)
