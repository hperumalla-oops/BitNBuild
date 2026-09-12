"""Extract page-by-page text from PDFs using pdfplumber."""

from pathlib import Path

import pdfplumber


def extract_pages(pdf_path: Path) -> list[dict]:
    """Returns a list of {"page_number": int (1-indexed), "text": str}."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append({"page_number": i + 1, "text": text})
    return pages


def full_text(pages: list[dict]) -> str:
    return "\n".join(p["text"] for p in pages)
