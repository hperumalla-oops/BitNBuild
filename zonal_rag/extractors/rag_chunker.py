"""Chunk regulatory text into RAG-ready segments and embed them with OpenAI.

Chunking itself is regex/heuristic (no LLM). Embeddings use OpenAI's
text-embedding-3-small (1536-dim, matches migrations/001_zoning_rag.sql).

If no source text is available at all, falls back to a handful of
LLM-fabricated synthetic chunks, flagged via is_synthetic=True — see
extractors/table_extractor.py for the same pattern on the tabular side.
"""

import asyncio
import json
import re

from extractors.table_extractor import _get_semaphore, get_openai_client

EMBEDDING_MODEL = "text-embedding-3-small"
MAX_CHUNK_TOKENS = 800  # approx, via char-length heuristic (~4 chars/token)
MAX_CHUNK_CHARS = MAX_CHUNK_TOKENS * 4

SECTION_HEADING_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)*\.?|[ivx]+\.)\s+(?P<title>.+)$",
    re.MULTILINE,
)


def chunk_pages(pages: list[dict], source_id: str, section: str) -> list[dict]:
    """Split concatenated page text on clause/section-number boundaries."""
    chunks = []
    seq = 0

    for page in pages:
        text = page["text"]
        if not text.strip():
            continue

        matches = list(SECTION_HEADING_RE.finditer(text))
        if not matches:
            seq += 1
            chunks.append(
                _make_chunk(source_id, section, seq, text, None, None, page["page_number"], 0, len(text))
            )
            continue

        for idx, match in enumerate(matches):
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            clause_text = text[start:end].strip()
            clause_number = match.group("num").rstrip(".")
            clause_title = match.group("title").strip()[:120]

            for sub_start, sub_text in _split_long_clause(clause_text):
                seq += 1
                chunks.append(
                    _make_chunk(
                        source_id,
                        section,
                        seq,
                        sub_text,
                        clause_number,
                        clause_title,
                        page["page_number"],
                        start + sub_start,
                        start + sub_start + len(sub_text),
                    )
                )

    return chunks


def _split_long_clause(text: str) -> list[tuple[int, str]]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [(0, text)]

    parts = []
    paragraphs = re.split(r"\n\s*\n", text)
    offset = 0
    buf = ""
    buf_start = 0
    for para in paragraphs:
        if buf and len(buf) + len(para) > MAX_CHUNK_CHARS:
            parts.append((buf_start, buf.strip()))
            buf_start = offset
            buf = para
        else:
            if not buf:
                buf_start = offset
            buf += ("\n\n" if buf else "") + para
        offset += len(para) + 2
    if buf.strip():
        parts.append((buf_start, buf.strip()))
    return parts


def _make_chunk(source_id, section, seq, text, clause_number, clause_title, page_number, start, end) -> dict:
    return {
        "chunk_id": f"{source_id}__{section}__{seq:03d}",
        "source_id": source_id,
        "section": section,
        "clause_number": clause_number,
        "clause_title": clause_title,
        "text": text,
        "page_number": page_number,
        "char_offset_start": start,
        "char_offset_end": end,
    }


async def embed_chunks(chunks: list[dict], source_url: str) -> list[dict]:
    """Attach source_url and an OpenAI embedding vector to each chunk dict.
    Fires all embedding batches concurrently (gated by the shared semaphore)."""
    if not chunks:
        return chunks
    client = get_openai_client()
    semaphore = _get_semaphore()
    batch_size = 100
    batches = [chunks[i : i + batch_size] for i in range(0, len(chunks), batch_size)]

    async def embed_batch(batch: list[dict]) -> list[dict]:
        texts = [c["text"] for c in batch]
        async with semaphore:
            response = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        for chunk, item in zip(batch, response.data):
            chunk["source_url"] = source_url
            chunk["embedding"] = item.embedding
        return batch

    results = await asyncio.gather(*(embed_batch(b) for b in batches))
    return [chunk for batch in results for chunk in batch]


async def synthetic_chunks(source_id: str, source_url: str, section: str, description: str) -> list[dict]:
    """Fabricate a handful of plausible RAG chunks when no source text is available."""
    client = get_openai_client()
    semaphore = _get_semaphore()
    prompt = f"""You are generating SYNTHETIC placeholder text for a zoning-regulation RAG demo
because the real source document ({description}) could not be retrieved or parsed.

Fabricate 3-5 short paragraphs (2-4 sentences each) of plausible Bangalore (BBMP) zoning
regulation text for the "{section}" section. Return ONLY a JSON array of objects:
[{{"clause_number": "2.1" or null, "clause_title": "short title" or null, "text": "..."}}]
No markdown, no explanation, no code fences."""

    async with semaphore:
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            max_completion_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    content = response.choices[0].message.content.strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    items = json.loads(content)

    chunks = []
    for seq, item in enumerate(items, start=1):
        chunks.append(
            _make_chunk(
                source_id,
                section,
                seq,
                item["text"],
                item.get("clause_number"),
                item.get("clause_title"),
                None,
                0,
                len(item["text"]),
            )
        )
    return await embed_chunks(chunks, source_url)
