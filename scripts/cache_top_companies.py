from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

from meridian.config import settings
from meridian.pipeline import (
    _has_cached_filing_coverage,
    _ingest_sec_filing,
    _ingest_xbrl,
)
from meridian.registry import CompanyRegistry
from meridian.sec import SecClient
from meridian.store import Store

def main():
    store = Store(settings.db_path)
    registry = CompanyRegistry(settings.universe_path)
    sec = SecClient(settings.sec_user_agent, settings.sec_min_interval)
    year = 2025
    years = [year]
    forms = ("10-K",)

    top25 = [c for c in registry.companies if c.cik][:25]
    print(f"==================================================", flush=True)
    print(f"Pre-caching Top {len(top25)} Companies for FY{year}", flush=True)
    print(f"==================================================", flush=True)

    done_count = 0
    cached_count = 0

    for idx, company in enumerate(top25, 1):
        t0 = time.perf_counter()
        has_filings = _has_cached_filing_coverage(store, company.cik, years, forms)
        has_facts = bool(store.cached_xbrl_facts(company.cik, [f"FY{year}"]))

        if has_filings and has_facts:
            cached_count += 1
            print(f"[{idx:2d}/{len(top25)}] {company.company_name} ({company.ticker or company.cik}) -> ALREADY CACHED (skipped)", flush=True)
            continue

        print(f"[{idx:2d}/{len(top25)}] Ingesting {company.company_name} ({company.ticker or company.cik})...", end="", flush=True)
        try:
            # 1. Download & ingest 10-K filing if missing
            if not has_filings:
                filings = sec.filings(company.cik, forms, None)
                selected = []
                for filing in filings:
                    report_date = filing.get("period", "")
                    fiscal_year = filing.get("fiscal_year")
                    y = int(fiscal_year) if fiscal_year and str(fiscal_year).isdigit() else (int(report_date[:4]) if report_date else int(filing.get("filed", "0")[:4] or 0))
                    if y in years or (y - 1 in years and filing.get("fiscal_period") == "FY"):
                        selected.append(filing)
                if selected:
                    selected.sort(key=lambda f: f.get("filed", ""), reverse=True)
                    target_filing = selected[0]
                    _ingest_sec_filing(store, sec, company, target_filing)

            # 2. Ingest XBRL facts if missing
            if not has_facts:
                raw_facts = sec.companyfacts(company.cik)
                filing_map = {d.get("accession", ""): {"url": d.get("source_url"),
                    "form": d.get("document_type"), "period": d.get("fiscal_period")}
                    for d in store.list_documents([company.cik]) if d.get("accession")}
                _ingest_xbrl(store, sec, company, year, raw_facts, filing_map)

            dt = time.perf_counter() - t0
            done_count += 1
            print(f" DONE in {dt:.1f}s", flush=True)
        except Exception as exc:
            print(f" ERROR: {exc}", flush=True)

    print("==================================================", flush=True)
    print(f"Pre-caching complete! Total top 25 companies ready: {cached_count + done_count}/{len(top25)}", flush=True)

if __name__ == "__main__":
    main()
