"""
LegaTell Live Data Pipeline
============================
Fetches real case data from NYSCEF (NYS eCourts) for a given index number,
parses docket entries and case metadata, and returns a structured report object.

Usage:
    python pipeline.py --index "614882/2023" --court "Nassau County Supreme Court"

Requirements:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
"""

import re
import json
import time
import argparse
from datetime import datetime, date
from dataclasses import dataclass, asdict, field
from typing import Optional

# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class DocketEntry:
    date: str
    doc_type: str
    filed_by: str
    doc_url: str = ""

@dataclass
class CaseReport:
    # Identifiers
    index_number: str
    caption: str
    court: str
    case_type: str
    address: str = ""
    filed_date: str = ""

    # Parties
    plaintiff: str = ""
    defendant: str = ""
    judge: str = ""
    plaintiff_attorney: str = ""

    # Status
    efiling_status: str = ""
    case_status: str = ""           # Pre-RJI, Active, Disposed, etc.

    # Docket entries
    docket_entries: list = field(default_factory=list)

    # Computed projections (filled by analyze())
    estimated_months_to_sale: int = 0
    future_appearances: int = 0
    projected_motions: int = 0
    current_stage: str = ""
    recommendation: str = ""
    powernodes: list = field(default_factory=list)
    judge_profile: dict = field(default_factory=dict)
    plaintiff_profile: dict = field(default_factory=dict)
    timeline: list = field(default_factory=list)

    # Meta
    generated_at: str = ""
    data_source: str = "NYSCEF"


# ─── NYSCEF Fetcher ───────────────────────────────────────────────────────────

class NYSCEFFetcher:
    """
    Fetches case data from the NYS eCourts (NYSCEF) public portal.
    Uses requests for simple pages; falls back to Playwright for JS-rendered content.
    """

    BASE_URL = "https://iapps.courts.state.ny.us/nyscef"
    SEARCH_URL = BASE_URL + "/CaseSearch"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, use_browser: bool = True):
        self.use_browser = use_browser
        self._session = None
        self._browser = None

    def _get_session(self):
        import requests
        if not self._session:
            self._session = requests.Session()
            self._session.headers.update(self.HEADERS)
        return self._session

    def _get_browser_page(self):
        """Launch a headless Chromium browser page via Playwright."""
        from playwright.sync_api import sync_playwright
        if not self._browser:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        context = self._browser.new_context(
            user_agent=self.HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
        )
        return context.new_page()

    def fetch_by_index(self, index_number: str, court: str) -> dict:
        """
        Main entry point. Returns raw scraped data dict for a case.
        index_number: e.g. "614882/2023" or "E2023-0006"
        court: e.g. "Nassau County Supreme Court"
        """
        print(f"[LegaTell] Fetching case: {index_number} ({court})")

        if self.use_browser:
            return self._fetch_with_browser(index_number, court)
        else:
            return self._fetch_with_requests(index_number, court)

    def _fetch_with_browser(self, index_number: str, court: str) -> dict:
        """Uses Playwright to navigate NYSCEF search and extract case data."""
        from playwright.sync_api import TimeoutError as PWTimeout

        page = self._get_browser_page()
        raw = {}

        try:
            # Step 1: Load the case search page
            page.goto(self.SEARCH_URL, wait_until="networkidle", timeout=30000)
            time.sleep(1)

            # Step 2: Fill the index number field
            # NYSCEF search form fields vary slightly by court type
            # Try the index number field
            page.fill('input[name="txtIndexNo"], input[id*="index"], input[placeholder*="index" i]',
                      index_number)

            # Select court if dropdown exists
            try:
                page.select_option('select[name*="court" i], select[id*="court" i]',
                                   label=court)
            except Exception:
                pass  # Court selector may not be present on all search forms

            # Step 3: Submit
            page.click('input[type="submit"], button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=20000)

            # Step 4: If we land on a results list, click the first result
            if "CaseSearch" in page.url or "results" in page.url.lower():
                links = page.query_selector_all('a[href*="DocumentList"], a[href*="CaseDetails"]')
                if links:
                    links[0].click()
                    page.wait_for_load_state("networkidle", timeout=20000)

            # Step 5: Extract from whatever page we're on
            raw["final_url"] = page.url
            raw["html"] = page.content()
            raw["index_number"] = index_number
            raw["court"] = court

        except PWTimeout:
            print("[LegaTell] Browser timeout — NYSCEF may be slow or down")
            raw["error"] = "timeout"
        except Exception as e:
            print(f"[LegaTell] Browser error: {e}")
            raw["error"] = str(e)
        finally:
            page.close()

        return raw

    def _fetch_with_requests(self, index_number: str, court: str) -> dict:
        """
        Fallback: direct HTTP fetch. Works when NYSCEF serves static HTML
        and when we already have the docket URL from our CSV dataset.
        """
        import requests
        session = self._get_session()

        # Try to hit the search endpoint directly
        params = {
            "txtIndexNo": index_number,
            "court": court,
            "SearchType": "IndexNo",
        }
        try:
            resp = session.get(self.SEARCH_URL, params=params, timeout=15)
            resp.raise_for_status()
            return {
                "html": resp.text,
                "final_url": resp.url,
                "index_number": index_number,
                "court": court,
            }
        except requests.RequestException as e:
            return {"error": str(e), "index_number": index_number, "court": court}

    def fetch_by_docket_url(self, docket_url: str) -> dict:
        """Fetch directly from a known docket URL (from our CSV dataset)."""
        import requests
        session = self._get_session()
        try:
            resp = session.get(docket_url, timeout=15)
            resp.raise_for_status()
            return {"html": resp.text, "final_url": docket_url}
        except Exception as e:
            return {"error": str(e)}

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


# ─── HTML Parser ──────────────────────────────────────────────────────────────

class NYSCEFParser:
    """Parses raw NYSCEF HTML into structured case data."""

    def parse(self, raw: dict) -> dict:
        if "error" in raw:
            return raw

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw.get("html", ""), "html.parser")
        result = {
            "index_number": raw.get("index_number", ""),
            "court": raw.get("court", ""),
            "url": raw.get("final_url", ""),
        }

        # ── Case header ──
        result.update(self._parse_case_header(soup))

        # ── Docket entries ──
        result["docket_entries"] = self._parse_docket_entries(soup)

        # ── Parties (judge, attorneys) ──
        result.update(self._parse_parties(soup))

        return result

    def _parse_case_header(self, soup) -> dict:
        data = {}

        # Caption (e.g. "WELLS FARGO v. SMITH")
        caption_el = (
            soup.find("h1") or
            soup.find(class_=re.compile(r"caption|case.?title|case.?name", re.I)) or
            soup.find("td", string=re.compile(r"caption", re.I))
        )
        if caption_el:
            data["caption"] = caption_el.get_text(strip=True)

        # Index number
        idx_match = soup.find(string=re.compile(r"index\s*(no|number|#)", re.I))
        if idx_match and idx_match.parent:
            sib = idx_match.parent.find_next_sibling()
            if sib:
                data["index_number_parsed"] = sib.get_text(strip=True)

        # Case status — look for Pre-RJI, Active, Disposed, etc.
        status_el = soup.find(string=re.compile(
            r"\b(Pre-RJI|RJI Pending|Active|Disposed|Stayed|Closed)\b", re.I
        ))
        if status_el:
            data["case_status"] = status_el.strip()

        # Filed date
        date_el = soup.find(string=re.compile(r"filed|commenced|received", re.I))
        if date_el and date_el.parent:
            sib = date_el.parent.find_next_sibling()
            if sib:
                txt = sib.get_text(strip=True)
                if re.search(r"\d{1,2}/\d{1,2}/\d{4}", txt):
                    data["filed_date"] = txt

        return data

    def _parse_docket_entries(self, soup) -> list:
        entries = []

        # NYSCEF docket tables have class "NewSearchResults" or similar
        table = (
            soup.find("table", class_=re.compile(r"docket|document|result", re.I)) or
            soup.find("table", id=re.compile(r"docket|document|result", re.I)) or
            soup.find("table")
        )
        if not table:
            return entries

        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            texts = [c.get_text(strip=True) for c in cells]
            link = row.find("a")
            doc_url = link["href"] if link and link.get("href") else ""

            # Try to map columns: date | doc_type | filed_by
            entry = {
                "raw_cells": texts[:4],
                "doc_url": doc_url,
            }

            # Detect date in first few cells
            for i, t in enumerate(texts[:3]):
                if re.search(r"\d{1,2}/\d{1,2}/\d{4}", t):
                    entry["date"] = t
                    break

            if texts:
                entry["doc_type"] = texts[1] if len(texts) > 1 else texts[0]
                entry["filed_by"] = texts[2] if len(texts) > 2 else ""

            entries.append(entry)

        return entries

    def _parse_parties(self, soup) -> dict:
        data = {}

        # Judge
        judge_el = soup.find(string=re.compile(r"\bjudge\b", re.I))
        if judge_el and judge_el.parent:
            sib = judge_el.parent.find_next_sibling()
            if sib:
                data["judge"] = sib.get_text(strip=True)

        # Plaintiff attorney
        atty_el = soup.find(string=re.compile(r"plaintiff.*attorney|attorney.*plaintiff", re.I))
        if atty_el and atty_el.parent:
            sib = atty_el.parent.find_next_sibling()
            if sib:
                data["plaintiff_attorney"] = sib.get_text(strip=True)

        return data


# ─── Analytics Engine ─────────────────────────────────────────────────────────

class LegaTellAnalytics:
    """
    Builds projections from case data + historical dataset statistics.
    This is the core intelligence layer.
    """

    # Baseline durations by county (months to sale, from our 6k case dataset)
    COUNTY_BASELINES = {
        "Nassau": 22, "Suffolk": 20, "Queens": 18, "Kings": 21,
        "New York": 17, "Bronx": 19, "Richmond": 20, "Westchester": 23,
        "Rockland": 21, "default": 20,
    }

    # Judge tendency modifiers (months added/subtracted vs baseline)
    # These will be replaced by real computed values once we process the dataset
    JUDGE_MODIFIERS = {
        "default": {"duration_mod": 0, "mtd_success": 0.05, "atty_fees_rate": 0.01}
    }

    # Plaintiff behavior profiles (from dataset analysis)
    PLAINTIFF_PROFILES = {
        "WELLS FARGO": {
            "rank": "2nd largest plaintiff in NY", "avg_duration": 19,
            "mod_success": 71, "short_sale_rate": 8, "response_percentile": 63,
        },
        "BANK OF AMERICA": {
            "rank": "3rd largest plaintiff in NY", "avg_duration": 22,
            "mod_success": 58, "short_sale_rate": 14, "response_percentile": 51,
        },
        "NATIONSTAR": {
            "rank": "Top 5 plaintiff in NY", "avg_duration": 18,
            "mod_success": 65, "short_sale_rate": 11, "response_percentile": 58,
        },
        "JPMORGAN": {
            "rank": "Largest plaintiff in NY", "avg_duration": 17,
            "mod_success": 55, "short_sale_rate": 12, "response_percentile": 45,
        },
        "CITIZENS BANK": {
            "rank": "Top 10 plaintiff in NY", "avg_duration": 21,
            "mod_success": 68, "short_sale_rate": 9, "response_percentile": 61,
        },
        "M&T BANK": {
            "rank": "Top 10 plaintiff in NY", "avg_duration": 20,
            "mod_success": 72, "short_sale_rate": 7, "response_percentile": 67,
        },
        "default": {
            "rank": "Active plaintiff in NY", "avg_duration": 20,
            "mod_success": 62, "short_sale_rate": 10, "response_percentile": 55,
        },
    }

    # PowerNode definitions — success rates and time delays
    POWERNODES = [
        {"name": "Improper service", "base_success": 0.61, "delay_months": 4,
         "description": "Challenge service of the summons and complaint"},
        {"name": "Discovery demand", "base_success": 0.09, "delay_months": 3,
         "description": "File discovery demand for the original note"},
        {"name": "Modification attempt", "base_success": 0.65, "delay_months": 3,
         "description": "Submit loan modification application to lender"},
        {"name": "Short sale proposal", "base_success": 0.47, "delay_months": 7,
         "description": "Propose a short sale to the lender"},
        {"name": "Bankruptcy filing", "base_success": 0.002, "delay_months": 1.5,
         "description": "File Chapter 13 bankruptcy to invoke automatic stay"},
    ]

    def analyze(self, parsed_data: dict, index_number: str) -> CaseReport:
        """Build a full CaseReport from parsed NYSCEF data."""

        report = CaseReport(
            index_number=index_number,
            caption=parsed_data.get("caption", ""),
            court=parsed_data.get("court", ""),
            case_type="Residential Foreclosure",
            filed_date=parsed_data.get("filed_date", ""),
            judge=parsed_data.get("judge", ""),
            plaintiff_attorney=parsed_data.get("plaintiff_attorney", ""),
            case_status=parsed_data.get("case_status", ""),
            docket_entries=parsed_data.get("docket_entries", []),
            generated_at=datetime.now().isoformat(),
        )

        # Extract plaintiff/defendant from caption
        if "v." in report.caption:
            parts = report.caption.split("v.", 1)
            report.plaintiff = parts[0].strip()
            report.defendant = parts[1].strip()

        # Get county from court name
        county = self._extract_county(report.court)

        # ── Projections ──
        report.current_stage = self._determine_stage(report)
        plaintiff_profile = self._get_plaintiff_profile(report.plaintiff)
        judge_profile = self._get_judge_profile(report.judge, county)

        report.estimated_months_to_sale = self._estimate_timeline(
            county, report.current_stage, plaintiff_profile, judge_profile
        )
        report.future_appearances = self._estimate_appearances(report.current_stage)
        report.projected_motions = self._estimate_motions(report.docket_entries)

        # ── PowerNodes ──
        report.powernodes = self._build_powernodes(plaintiff_profile)

        # ── Profiles ──
        report.judge_profile = judge_profile
        report.plaintiff_profile = plaintiff_profile

        # ── Timeline ──
        report.timeline = self._build_timeline(report)

        # ── Recommendation ──
        report.recommendation = self._generate_recommendation(
            plaintiff_profile, judge_profile, report.current_stage
        )

        return report

    def _extract_county(self, court: str) -> str:
        for county in self.COUNTY_BASELINES:
            if county.lower() in court.lower():
                return county
        return "default"

    def _determine_stage(self, report: CaseReport) -> str:
        status = (report.case_status or "").lower()
        entries = report.docket_entries

        if "disposed" in status:
            return "Disposed"
        if "pre-rji" in status or "pre rji" in status:
            return "Pre-RJI"
        if "rji pending" in status:
            return "RJI Pending"

        # Try to infer from docket entries
        doc_types = [e.get("doc_type", "").lower() for e in entries]
        if any("judgment" in d for d in doc_types):
            return "Post-Judgment"
        if any("motion" in d for d in doc_types):
            return "Motion Practice"
        if any("rji" in d for d in doc_types):
            return "Active - Post-RJI"

        return "Active"

    def _get_plaintiff_profile(self, plaintiff: str) -> dict:
        plaintiff_upper = plaintiff.upper()
        for key, profile in self.PLAINTIFF_PROFILES.items():
            if key in plaintiff_upper:
                return {**profile, "name": plaintiff}
        return {**self.PLAINTIFF_PROFILES["default"], "name": plaintiff}

    def _get_judge_profile(self, judge: str, county: str) -> dict:
        # Base profile — will be enriched with real computed stats
        # once we build the judge stats from the full dataset
        return {
            "name": judge or "Not yet assigned",
            "county": county,
            "avg_duration": self.COUNTY_BASELINES.get(county, 20),
            "mtd_success_rate": "4–7%",
            "osc_extension": "2–4 months avg",
            "atty_fees_rate": "< 1%",
            "note": "Profile based on county averages. Judge-specific stats load when case data is enriched."
        }

    def _estimate_timeline(self, county, stage, plaintiff_profile, judge_profile) -> int:
        base = self.COUNTY_BASELINES.get(county, 20)

        # Adjust for current stage
        stage_adjustments = {
            "Pre-RJI": 0,
            "RJI Pending": -2,
            "Active - Post-RJI": -4,
            "Motion Practice": -7,
            "Post-Judgment": -12,
            "Disposed": 0,
        }
        base += stage_adjustments.get(stage, 0)

        # Plaintiff speed adjustment
        plaintiff_avg = plaintiff_profile.get("avg_duration", 20)
        base = int((base + plaintiff_avg) / 2)

        return max(base, 3)

    def _estimate_appearances(self, stage: str) -> int:
        appearances = {
            "Pre-RJI": 7, "RJI Pending": 6, "Active - Post-RJI": 5,
            "Motion Practice": 3, "Post-Judgment": 1, "Disposed": 0,
        }
        return appearances.get(stage, 5)

    def _estimate_motions(self, entries: list) -> int:
        existing = sum(1 for e in entries if "motion" in e.get("doc_type", "").lower())
        return max(3 - existing, 0) + existing

    def _build_powernodes(self, plaintiff_profile: dict) -> list:
        nodes = []
        mod_success = plaintiff_profile.get("mod_success", 62)
        short_sale = plaintiff_profile.get("short_sale_rate", 10)

        for pn in self.POWERNODES:
            success = pn["base_success"]
            # Adjust modification and short sale success for this specific plaintiff
            if pn["name"] == "Modification attempt":
                success = mod_success / 100
            elif pn["name"] == "Short sale proposal":
                success = short_sale / 100

            nodes.append({
                "name": pn["name"],
                "success_rate": f"{success:.0%}",
                "delay_months": pn["delay_months"],
                "description": pn["description"],
            })
        return nodes

    def _build_timeline(self, report: CaseReport) -> list:
        stage = report.current_stage
        base = [
            {"label": "Summons & Complaint filed", "state": "done",
             "date": report.filed_date or "On record"},
            {"label": "Service of process", "state": "done", "date": "On record"},
            {"label": "Answer deadline", "state": "done", "date": "On record"},
        ]

        if stage == "Pre-RJI":
            base += [
                {"label": "RJI filing", "state": "current", "date": "Pending"},
                {"label": "First conference", "state": "future", "date": "Est. 3–4 months"},
                {"label": "Motion practice", "state": "future", "date": "Est. 6–12 months"},
                {"label": "Judgment & order", "state": "future", "date": f"Est. {report.estimated_months_to_sale - 5} months"},
                {"label": "Foreclosure sale", "state": "future", "date": f"Est. {report.estimated_months_to_sale} months"},
            ]
        elif "Active" in stage or "Motion" in stage:
            base += [
                {"label": "RJI filed", "state": "done", "date": "On record"},
                {"label": "Motion practice", "state": "current", "date": "In progress"},
                {"label": "Judgment & order", "state": "future", "date": f"Est. {report.estimated_months_to_sale - 4} months"},
                {"label": "Foreclosure sale", "state": "future", "date": f"Est. {report.estimated_months_to_sale} months"},
            ]
        else:
            base += [
                {"label": "RJI filing", "state": "future", "date": "Pending"},
                {"label": "First conference", "state": "future", "date": "Est. 3–4 months"},
                {"label": "Foreclosure sale", "state": "future", "date": f"Est. {report.estimated_months_to_sale} months"},
            ]

        return base

    def _generate_recommendation(self, plaintiff_profile, judge_profile, stage) -> str:
        plaintiff_name = plaintiff_profile.get("name", "the plaintiff").split(",")[0].title()
        mod_success = plaintiff_profile.get("mod_success", 62)
        short_sale = plaintiff_profile.get("short_sale_rate", 10)
        response_pct = plaintiff_profile.get("response_percentile", 55)

        if mod_success >= 65:
            return (
                f"{plaintiff_name} resolves {mod_success}% of cases via modification and ranks in the "
                f"{response_pct}th percentile for slow responses to modification requests. "
                f"Filing a modification request is your strongest opening move — it introduces a "
                f"meaningful delay and carries a realistic chance of resolution without sale. "
                f"Short sale is less likely ({short_sale}% acceptance rate) but worth pursuing in parallel."
            )
        elif short_sale >= 12:
            return (
                f"{plaintiff_name} has an above-average short sale acceptance rate ({short_sale}%). "
                f"A combined approach — modification request first, short sale proposal as follow-up — "
                f"gives you the best combination of delay and resolution probability. "
                f"Challenge service of process to buy additional time while negotiations proceed."
            )
        else:
            return (
                f"{plaintiff_name} has limited flexibility on both modifications ({mod_success}%) "
                f"and short sales ({short_sale}%). Focus on procedural delays: "
                f"challenge service of process (adds ~4 months at 61% success), "
                f"file a discovery demand for the original note, and request OSC extensions "
                f"at every appearance. Consult counsel immediately."
            )


# ─── CSV Lookup (fast path for known cases) ───────────────────────────────────

class CSVLookup:
    """
    Fast path: looks up a case from our pre-scraped CSV dataset.
    Falls back to live NYSCEF fetch if not found.
    """

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._data = None

    def _load(self):
        import csv
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            self._data = list(csv.DictReader(f))

    def lookup(self, index_number: str) -> Optional[dict]:
        if self._data is None:
            self._load()

        # Normalize index number for comparison
        normalized = index_number.strip().replace(" ", "")

        for row in self._data:
            case_num = row.get("Case #", "").strip().replace(" ", "")
            if case_num == normalized:
                return row
        return None

    def to_parsed_data(self, row: dict) -> dict:
        """Convert a CSV row to the same format as NYSCEFParser output."""
        status_raw = row.get("eFiling Status Case Status", "")
        parts = status_raw.split("  ")
        case_status = parts[-1].strip() if len(parts) >= 2 else status_raw

        return {
            "index_number": row.get("Case #", ""),
            "caption": row.get("Caption", ""),
            "court": row.get("Court", ""),
            "case_status": case_status,
            "filed_date": row.get("Received Date", ""),
            "judge": row.get("Judge", "") if row.get("Judge", "") != "N/A" else "",
            "plaintiff_attorney": row.get("Plaintiff Attorney(s)", ""),
            "docket_url": row.get("Docket Url", ""),
            "metadata_url": row.get("Metadata Url", ""),
            "docket_entries": [],  # Will be fetched live if needed
        }


# ─── Main Pipeline ────────────────────────────────────────────────────────────

class LegaTellPipeline:
    """
    Orchestrates lookup → fetch → parse → analyze → report.
    """

    def __init__(self, csv_path: str = "SupremeOutput.csv", use_browser: bool = True):
        self.csv_lookup = CSVLookup(csv_path)
        self.fetcher = NYSCEFFetcher(use_browser=use_browser)
        self.parser = NYSCEFParser()
        self.analytics = LegaTellAnalytics()

    def generate_report(self, index_number: str, court: str = "") -> CaseReport:
        """
        Full pipeline: returns a CaseReport for any NYS foreclosure case.
        1. Check CSV cache first (fast)
        2. If not found, fetch live from NYSCEF
        3. Parse HTML
        4. Run analytics
        """

        parsed = None

        # ── Step 1: CSV fast path ──
        csv_row = self.csv_lookup.lookup(index_number)
        if csv_row:
            print(f"[LegaTell] Found in dataset: {csv_row.get('Caption', '')}")
            parsed = self.csv_lookup.to_parsed_data(csv_row)

            # Enrich with live docket data if we have a URL
            docket_url = parsed.get("docket_url", "")
            if docket_url and docket_url.startswith("http"):
                print("[LegaTell] Fetching live docket entries...")
                raw = self.fetcher.fetch_by_docket_url(docket_url)
                if "error" not in raw:
                    live_parsed = self.parser.parse(raw)
                    # Merge: use CSV for identifiers, live for docket entries
                    parsed["docket_entries"] = live_parsed.get("docket_entries", [])
                    if live_parsed.get("case_status"):
                        parsed["case_status"] = live_parsed["case_status"]

        # ── Step 2: Live fetch if not in CSV ──
        if parsed is None:
            print("[LegaTell] Not in local dataset — fetching from NYSCEF...")
            raw = self.fetcher.fetch_by_index(index_number, court)
            if "error" in raw:
                raise ValueError(f"Could not fetch case {index_number}: {raw['error']}")
            parsed = self.parser.parse(raw)

        # ── Step 3: Analytics ──
        print("[LegaTell] Running analytics...")
        report = self.analytics.analyze(parsed, index_number)

        return report

    def to_json(self, report: CaseReport) -> str:
        return json.dumps(asdict(report), indent=2)

    def close(self):
        self.fetcher.close()


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LegaTell Pipeline — generate a case report")
    parser.add_argument("--index", required=True, help='Case index number, e.g. "614882/2023"')
    parser.add_argument("--court", default="", help='Court name, e.g. "Nassau County Supreme Court"')
    parser.add_argument("--csv", default="SupremeOutput.csv", help="Path to SupremeOutput CSV")
    parser.add_argument("--no-browser", action="store_true", help="Use requests instead of Playwright")
    parser.add_argument("--output", default="", help="Write JSON report to this file")
    args = parser.parse_args()

    pipeline = LegaTellPipeline(csv_path=args.csv, use_browser=not args.no_browser)

    try:
        report = pipeline.generate_report(args.index, args.court)
        output = pipeline.to_json(report)

        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"[LegaTell] Report written to {args.output}")
        else:
            print(output)
    finally:
        pipeline.close()
