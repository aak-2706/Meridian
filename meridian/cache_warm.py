from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from typing import Callable

from .pipeline import _has_cached_filing_coverage, _ingest_sec_filing, _ingest_xbrl
from .registry import Company
from .store import Store


def warm_company_cache(companies: list[Company], years: list[int], store: Store, sec,
                       progress: Callable[[dict], None], cancelled: Event | None = None) -> dict:
    """Cache annual SEC filings and reported facts for a company universe."""
    forms = ("10-K",)
    totals = {"companies_total": len(companies), "companies_done": 0,
              "filings_added": 0, "facts_added": 0, "errors": [],
              "coverage_gaps": []}
    for company in companies:
        if cancelled and cancelled.is_set():
            break
        progress({**totals, "company": company.company_name, "state": "running"})
        try:
            already_has_filings = _has_cached_filing_coverage(store, company.cik, years, forms)
            filings = []
            try:
                if not already_has_filings:
                    filings = sec.filings(company.cik, forms, None)
            except Exception as exc:
                totals["errors"].append({"company": company.company_name,
                    "error": f"filing discovery: {str(exc)[:400]}"})
            selected = []
            for filing in filings:
                report_date = filing.get("period", "")
                fiscal_year = filing.get("fiscal_year")
                year = int(fiscal_year) if fiscal_year and str(fiscal_year).isdigit() else (int(report_date[:4]) if report_date else int((filing.get("filed", "0")[:4] or 0)))
                if year in years or (year - 1 in years and filing.get("fiscal_period") == "FY"):
                    selected.append(filing)
            found_years = set(years) if already_has_filings else set()
            for filing in selected:
                report_date = filing.get("period", "")
                fiscal_year = filing.get("fiscal_year")
                report_year = int(fiscal_year) if fiscal_year and str(fiscal_year).isdigit() else (int(report_date[:4]) if report_date else int((filing.get("filed", "0")[:4] or 0)))
                target_year = report_year - 1 if report_year not in years and report_year - 1 in years and filing.get("fiscal_period") == "FY" else report_year
                if target_year in years:
                    found_years.add(target_year)
            missing_years_for_company = sorted(set(years) - found_years)
            if missing_years_for_company:
                totals["coverage_gaps"].append({"company": company.company_name,
                    "years": missing_years_for_company})
            unique = {}
            for filing in selected:
                unique[(filing.get("form"), filing.get("period"), filing.get("fiscal_period"))] = filing
            for filing in unique.values():
                if cancelled and cancelled.is_set():
                    break
                try:
                    if store.has_accession(company.cik, filing.get("accession", "")):
                        stored = store.document_by_accession(company.cik, filing.get("accession", "")) or {}
                        if "inline_xbrl_review" not in stored.get("metadata", {}):
                            _ingest_sec_filing(store, sec, company, filing)
                        continue
                    item = _ingest_sec_filing(store, sec, company, filing)
                    totals["filings_added"] += int(bool(item.get("created")))
                except Exception as exc:
                    totals["errors"].append({"company": company.company_name,
                        "error": f"filing download/index: {str(exc)[:400]}"})

            periods = [f"FY{year}" for year in years]
            cached = store.cached_xbrl_facts(company.cik, periods)
            missing_years = [year for year in years if f"FY{year}" not in cached]
            if missing_years and not (cancelled and cancelled.is_set()):
                try:
                    raw_facts = sec.companyfacts(company.cik)
                    accession_map = {f.get("accession", ""): f for f in unique.values() if f.get("accession")}
                    for year in missing_years:
                        result = _ingest_xbrl(store, sec, company, year, raw_facts, accession_map)
                        totals["facts_added"] += int(bool(result.get("created")))
                except Exception as exc:
                    totals["errors"].append({"company": company.company_name,
                        "error": f"XBRL facts: {str(exc)[:400]}"})
        except Exception as exc:
            totals["errors"].append({"company": company.company_name, "error": str(exc)[:500]})
        totals["companies_done"] += 1
        progress({**totals, "company": company.company_name, "state": "running"})

    state = "cancelled" if cancelled and cancelled.is_set() else "complete"
    result = {**totals, "state": state, "finished_at": datetime.now(timezone.utc).isoformat()}
    progress(result)
    return result
