#!/usr/bin/env python3
"""
Parse the scraped HTML represented by things.html into structured JSON.

Usage:
    python parse_things.py things.html things.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup


def clean(text: str) -> str:
    """Collapse HTML whitespace without changing the actual words."""
    return " ".join(text.split())


def has_classes(tag, required: set[str]) -> bool:
    classes = set(tag.get("class", []))
    return required.issubset(classes)


def parse_age(text: str):
    """
    Keep the original UI label and additionally parse numeric day ages.
    'Today' deliberately remains None rather than assuming a timestamp.
    """
    text = clean(text)
    match = re.fullmatch(r"(\d+)d ago", text, flags=re.I)
    return int(match.group(1)) if match else None


def parse_summary(text: str):
    text = clean(text)
    match = re.fullmatch(r"(\d+)\s+unresolved\s+·\s*(.*)", text, flags=re.I)
    if not match:
        return {
            "unresolved": None,
            "representative": None,
            "raw": text,
        }

    return {
        "unresolved": int(match.group(1)),
        "representative": match.group(2).strip(),
        "raw": text,
    }


def parse_zone_label(text: str):
    text = clean(text)
    match = re.fullmatch(r"(.+?)\s+#(\d+)", text)
    if not match:
        return {
            "label": text,
            "zone": None,
            "ward_number": None,
        }

    return {
        "label": text,
        "zone": match.group(1).strip(),
        "ward_number": int(match.group(2)),
    }


def parse_report(row, report_index: int):
    spans = row.find_all("span")
    count = None

    # The only span in a report row is the numeric report count.
    for span in spans:
        text = clean(span.get_text(" ", strip=True))
        if text.isdigit():
            count = int(text)
            break

    name_div = row.find(
        "div",
        class_=lambda c: c and "text-[13px]" in c and "truncate" in c,
    )
    age_div = row.find(
        "div",
        class_=lambda c: c and "text-[11px]" in c and "truncate" in c,
    )
    status_div = row.find(
        "div",
        class_=lambda c: c and "rounded-full" in c,
    )

    location = clean(name_div.get_text(" ", strip=True)) if name_div else None
    age_text = clean(age_div.get_text(" ", strip=True)) if age_div else None
    status = clean(status_div.get_text(" ", strip=True)) if status_div else None

    return {
        "report_index": report_index,
        "count": count,
        "location": location,
        "age": age_text,
        "age_days_ago": parse_age(age_text) if age_text else None,
        "status": status,
    }


def parse_html(input_file: Path) -> dict:
    html = input_file.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    sections = []

    # Each displayed section is a .border-b container whose first child is
    # the section button and whose optional second child is the expanded list.
    section_containers = soup.find_all(
        "div",
        class_=lambda c: c and "border-b" in c and "border-gray-100" in c,
    )

    for section_index, container in enumerate(section_containers, start=1):
        button = container.find("button", recursive=False)
        if button is None:
            continue

        spans = button.find_all("span")

        def span_with_class(fragment):
            for span in spans:
                if fragment in " ".join(span.get("class", [])):
                    return clean(span.get_text(" ", strip=True))
            return None

        total = None
        total_text = span_with_class("text-sm")
        if total_text and total_text.isdigit():
            total = int(total_text)

        name = span_with_class("font-semibold")
        zone_label = span_with_class("text-[10px]")

        summary_div = button.find(
            "div",
            class_=lambda c: c and "text-xs" in c and "truncate" in c,
        )
        summary = parse_summary(
            summary_div.get_text(" ", strip=True) if summary_div else ""
        )

        zone = parse_zone_label(zone_label or "")

        chevron = button.find("svg")
        expanded_by_chevron = bool(
            chevron and "rotate-180" in chevron.get("class", [])
        )

        child_container = container.find(
            "div",
            class_=lambda c: c and "bg-gray-50/50" in c,
            recursive=False,
        )

        reports = []
        if child_container is not None:
            for child in child_container.find_all("div", recursive=False):
                if not has_classes(child, {"px-5", "pl-[72px]", "cursor-pointer"}):
                    continue
                reports.append(parse_report(child, len(reports) + 1))

        status_counts = {}
        for report in reports:
            status = report["status"]
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1

        sections.append({
            "section_index": section_index,
            "name": name,
            "zone": zone["zone"],
            "ward_number": zone["ward_number"],
            "zone_label": zone["label"],
            "total": total,
            "unresolved": summary["unresolved"],
            "representative": summary["representative"],
            "expanded": expanded_by_chevron or child_container is not None,
            "report_count_in_html": len(reports),
            "status_counts_in_html": status_counts,
            "reports": reports,
        })

    return {
        "source_file": input_file.name,
        "parser": "parse_things.py",
        "sections": sections,
        "metadata": {
            "section_count": len(sections),
            "expanded_section_count": sum(1 for s in sections if s["expanded"]),
            "report_count_in_html": sum(s["report_count_in_html"] for s in sections),
        },
    }


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit(
            "Usage: python parse_things.py INPUT.html [OUTPUT.json]"
        )

    input_file = Path(sys.argv[1])
    output_file = (
        Path(sys.argv[2])
        if len(sys.argv) == 3
        else input_file.with_suffix(".json")
    )

    data = parse_html(input_file)
    output_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"Wrote {output_file} "
        f"({data['metadata']['section_count']} sections, "
        f"{data['metadata']['report_count_in_html']} expanded report rows)"
    )


if __name__ == "__main__":
    main()
