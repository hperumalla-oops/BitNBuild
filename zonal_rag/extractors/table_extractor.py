"""LLM-based structured extraction of regulatory tables from PDF text, using OpenAI.

All OpenAI calls are async and run concurrently (gated by a shared semaphore)
so that, e.g., the 8 tables extracted from the RMP 2015 source — each split
across several page-chunk calls — all fire in parallel instead of one at a
time.

If real extraction isn't possible (source text unavailable, or the model
returns nothing usable after retries), falls back to asking the model to
FABRICATE a small set of plausible example rows instead, clearly flagged
via is_synthetic=True on every row. This keeps the demo pipeline runnable
end-to-end even when a source document can't be reached or parsed, but
synthetic rows must never be mistaken for real regulatory data downstream.
"""

import asyncio
import json

from openai import AsyncOpenAI

MODEL = "gpt-5.4-mini"
MAX_RETRIES = 4
PAGES_PER_CHUNK = 8
MAX_CONCURRENT_CALLS = 6

_client: AsyncOpenAI | None = None
_semaphore: asyncio.Semaphore | None = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        import os

        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API"])
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    return _semaphore


EXTRACTION_PROMPT = """You are extracting structured data from Indian zoning regulations.

Here is the text from pages {start}-{end} of {source_description}. Each page's text is
preceded by a "[PAGE N]" marker:

<regulatory_text>
{extracted_text}
</regulatory_text>

Extract all rows matching this JSON schema (a list of objects with these fields):
{json_schema}

Rules:
- Extract every row you can find. Do not skip entries.
- Convert all measurements to metric (metres, sq.m). If source uses feet, convert.
- Fields named like "*_m" (front_setback_m, max_height_m, etc.) must hold an absolute measurement in metres. If the source expresses that value as a PERCENTAGE (e.g. "8% of plot width") rather than an absolute distance, leave the "*_m" field null and put the percentage rule as text in a "conditions"/"source" field if the schema has one — never write a raw percentage number (like 0.08 or 8) into a "*_m" field.
- If a cell says "as per table X", leave the value as null and note the reference in a "conditions" field if the schema has one.
- Include the source page number for each row (source_page) — use the "[PAGE N]" marker immediately preceding the text the row came from. Never guess or interpolate a page number.
- Only extract rows that genuinely match this table's subject matter. If a page-chunk contains no data relevant to this schema, return an empty array rather than force-fitting unrelated tables (e.g. a road building-line list is not parking data).
- Return ONLY a valid JSON array of row objects. No markdown, no explanation, no code fences.
"""

SYNTHETIC_PROMPT = """You are generating SYNTHETIC placeholder data for a zoning-regulation demo pipeline
because the real source document ({source_description}) could not be retrieved or parsed.

Fabricate a small set of plausible example rows (6-10) matching this JSON schema:
{json_schema}

This is example data for {table_name} in the context of Bangalore (BBMP) zoning regulations
(zones like R1/R2/C1/C2/M1, rings I/II/III where applicable). Make the values internally
consistent and realistic in magnitude, but do not claim they come from any specific page —
set source_page to null.

Return ONLY a valid JSON array of row objects. No markdown, no explanation, no code fences.
"""


async def _call_openai(prompt: str) -> list[dict]:
    client = get_openai_client()
    semaphore = _get_semaphore()
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            async with semaphore:
                response = await client.chat.completions.create(
                    model=MODEL,
                    max_completion_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
            content = response.choices[0].message.content.strip()
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(content)
        except Exception as e:  # rate limits, transient API errors, bad JSON
            last_error = e
            await asyncio.sleep(2**attempt)
    raise RuntimeError(f"OpenAI extraction failed after {MAX_RETRIES} attempts: {last_error}")


async def extract_table_rows(
    table_name: str,
    json_schema: dict,
    source_description: str,
    pages: list[dict],
) -> tuple[list[dict], bool]:
    """Returns (rows, is_synthetic). Fires all page-chunk extraction calls for
    this table concurrently, deduplicates, and falls back to synthetic
    generation if nothing comes back."""
    if pages:
        chunks = [pages[i : i + PAGES_PER_CHUNK] for i in range(0, len(pages), PAGES_PER_CHUNK)]
        chunks = [c for c in chunks if any(p["text"].strip() for p in c)]

        async def extract_chunk(chunk: list[dict]) -> list[dict]:
            # Explicit per-page markers so the model can attribute each row
            # to its real page instead of guessing from a blob of merged text.
            chunk_text = "\n".join(
                f"[PAGE {p['page_number']}]\n{p['text']}" for p in chunk if p["text"].strip()
            )
            prompt = EXTRACTION_PROMPT.format(
                start=chunk[0]["page_number"],
                end=chunk[-1]["page_number"],
                source_description=source_description,
                extracted_text=chunk_text,
                json_schema=json.dumps(json_schema, indent=2),
            )
            try:
                rows = await _call_openai(prompt)
                return rows if isinstance(rows, list) else []
            except RuntimeError:
                return []  # try the remaining page chunks before giving up entirely

        results = await asyncio.gather(*(extract_chunk(c) for c in chunks))
        all_rows = [row for chunk_rows in results for row in chunk_rows]

        deduped = _dedupe_rows(all_rows)
        if deduped:
            return deduped, False

    # Fallback: real extraction produced nothing usable.
    prompt = SYNTHETIC_PROMPT.format(
        source_description=source_description,
        json_schema=json.dumps(json_schema, indent=2),
        table_name=table_name,
    )
    rows = await _call_openai(prompt)
    return rows if isinstance(rows, list) else [], True


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped
