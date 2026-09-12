
import argparse
import json
import re
import time

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


URL = "https://www.nammakasa.in"


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def create_driver():
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_text(element):
    if not element:
        return None

    text = element.get_text(" ", strip=True)

    return text if text else None


def parse_int(text):
    if not text:
        return None

    match = re.search(r"\d[\d,]*", text)

    if not match:
        return None

    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_zone_label(zone_label):
    """
    Example:

        Central #42

    ->

        zone = Central
        ward_number = 42
    """

    if not zone_label:
        return None, None

    match = re.match(
        r"^(.*?)\s*#\s*(\d+)$",
        zone_label.strip()
    )

    if not match:
        return zone_label.strip(), None

    return (
        match.group(1).strip(),
        int(match.group(2))
    )


def parse_summary(summary):
    """
    Example:

        140 unresolved · Uday B. Garudachar

    ->

        unresolved = 140
        representative = Uday B. Garudachar
    """

    if not summary:
        return None, None

    unresolved = None
    representative = None

    parts = summary.split("·", 1)

    if parts:
        unresolved = parse_int(parts[0])

    if len(parts) == 2:
        representative = parts[1].strip()

    return unresolved, representative


# ---------------------------------------------------------------------------
# Report parser
# ---------------------------------------------------------------------------

def parse_reports(section):
    reports = []

    # Expanded report container
    report_container = section.find(
        "div",
        class_=lambda classes:
            classes and "bg-gray-50/50" in classes,
        recursive=False
    )

    if not report_container:
        return reports

    for row in report_container.find_all(
        "div",
        recursive=False
    ):

        # Only process actual report rows.
        #
        # The report rows from the page have:
        # px-5 py-3 pl-[72px]
        classes = row.get("class", [])

        if not all(
            cls in classes
            for cls in [
                "px-5",
                "py-3",
                "pl-[72px]"
            ]
        ):
            continue

        # Report count
        count_el = row.find(
            "span",
            class_=lambda classes:
                classes
                and "font-extrabold" in classes
                and "font-mono" in classes
        )

        # Location
        location_el = row.find(
            "div",
            class_=lambda classes:
                classes and "text-[13px]" in classes
        )

        # Age
        age_el = row.find(
            "div",
            class_=lambda classes:
                classes and "text-[11px]" in classes
        )

        # Status
        status_el = row.find(
            "div",
            class_=lambda classes:
                classes
                and "inline-flex" in classes
                and "rounded-full" in classes
        )

        reports.append({
            "count": parse_int(clean_text(count_el)),
            "location": clean_text(location_el),
            "age": clean_text(age_el),
            "status": clean_text(status_el),
        })

    return reports


# ---------------------------------------------------------------------------
# Section parser
# ---------------------------------------------------------------------------

def parse_section(section, section_index):

    button = section.find(
        "button",
        recursive=False
    )

    if not button:
        return None

    # Total
    total_el = button.find(
        "span",
        class_=lambda classes:
            classes
            and "font-extrabold" in classes
            and "font-mono" in classes
            and "text-sm" in classes
    )

    total = parse_int(clean_text(total_el))

    # Name
    name_el = button.find(
        "span",
        class_=lambda classes:
            classes
            and "text-sm" in classes
            and "font-semibold" in classes
    )

    name = clean_text(name_el)

    # Zone / ward label
    zone_el = button.find(
        "span",
        class_=lambda classes:
            classes and any(
                "text-[10px]" == cls
                for cls in classes
            )
    )

    zone_label = clean_text(zone_el)

    zone, ward_number = parse_zone_label(zone_label)

    # Summary
    summary_el = button.find(
        "div",
        class_=lambda classes:
            classes
            and "text-xs" in classes
            and "text-gray-400" in classes
            and "truncate" in classes
    )

    summary = clean_text(summary_el)

    unresolved, representative = parse_summary(summary)

    # Reports
    reports = parse_reports(section)

    return {
        "section_index": section_index,

        "name": name,

        "zone": zone,

        "ward_number": ward_number,

        "zone_label": zone_label,

        "total": total,

        "unresolved": unresolved,

        "representative": representative,

        "expanded": len(reports) > 0,

        "report_count_in_html": len(reports),

        "reports": reports,
    }


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape(args):

    driver = create_driver()

    try:
        print("Opening:", URL)

        driver.get(URL)

        wait = WebDriverWait(driver, 30)

        # ---------------------------------------------------------------
        # 1. Click "List"
        # ---------------------------------------------------------------

        print("Waiting for List button...")

        list_button = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[normalize-space()='List']"
                )
            )
        )

        print("Clicking List...")

        driver.execute_script(
            "arguments[0].click();",
            list_button
        )

        # ---------------------------------------------------------------
        # 2. Wait for highlighted list panel
        # ---------------------------------------------------------------

        print("Waiting for list panel...")

        panel_element = wait.until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "div.h-full.overflow-y-auto"
                )
            )
        )

        # Let React finish rendering
        time.sleep(args.wait)

        print("List panel found.")

        # ---------------------------------------------------------------
        # 3. Get HTML
        # ---------------------------------------------------------------

        html = panel_element.get_attribute("outerHTML")

        if not html:
            raise RuntimeError(
                "Could not retrieve list panel HTML."
            )

        # Optional: save raw highlighted HTML
        if args.html_output:
            with open(
                args.html_output,
                "w",
                encoding="utf-8"
            ) as f:
                f.write(html)

            print(
                "Saved highlighted HTML:",
                args.html_output
            )

        # ---------------------------------------------------------------
        # 4. Parse HTML
        # ---------------------------------------------------------------

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        panel = soup.select_one(
            "div.h-full.overflow-y-auto"
        )

        if panel is None:
            raise RuntimeError(
                "Could not find highlighted panel in retrieved HTML."
            )

        # The highlighted section consists of direct children:
        #
        # <div class="border-b border-gray-100">
        #
        section_elements = panel.find_all(
            "div",
            class_=lambda classes:
                classes
                and "border-b" in classes
                and "border-gray-100" in classes,
            recursive=False
        )

        print(
            "Found",
            len(section_elements),
            "ward sections."
        )

        sections = []

        for index, section_element in enumerate(
            section_elements
        ):

            section = parse_section(
                section_element,
                index
            )

            if section:
                sections.append(section)

        # ---------------------------------------------------------------
        # 5. Build output
        # ---------------------------------------------------------------

        result = {
            "source": URL,

            "scraped_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),

            "parser": "selenium + beautifulsoup",

            "sections": sections,

            "metadata": {
                "section_count": len(sections),

                "expanded_section_count": sum(
                    1
                    for section in sections
                    if section["expanded"]
                ),

                "report_count": sum(
                    len(section["reports"])
                    for section in sections
                ),
            }
        }

        # ---------------------------------------------------------------
        # 6. Save JSON
        # ---------------------------------------------------------------

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                result,
                f,
                indent=2,
                ensure_ascii=False
            )

        print()
        print("Done.")
        print("Sections:", len(sections))
        print(
            "Expanded sections:",
            result["metadata"]["expanded_section_count"]
        )
        print(
            "Reports:",
            result["metadata"]["report_count"]
        )
        print("JSON:", args.output)

        return result

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Scrape the NammaKasa List view "
            "and extract ward/report data."
        )
    )

    parser.add_argument(
        "--url",
        default=URL,
        help="NammaKasa URL"
    )

    parser.add_argument(
        "--output",
        default="nammakasa.json",
        help="Output JSON file"
    )

    parser.add_argument(
        "--html-output",
        default=None,
        help=(
            "Optional file to save the highlighted "
            "list panel HTML"
        )
    )

    parser.add_argument(
        "--wait",
        type=float,
        default=3,
        help="Seconds to wait after clicking List"
    )

    args = parser.parse_args()

    scrape(args)


if __name__ == "__main__":
    main()