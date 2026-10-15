from __future__ import annotations

import json
import re
import gzip
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


SEC_DATA = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


class SecClient:
    def __init__(self, user_agent: str, min_interval: float = 0.12):
        self.user_agent = user_agent.strip()
        self.min_interval = min_interval
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _fetch(self, url: str, binary: bool = False) -> bytes:
        if not self.user_agent or "@" not in self.user_agent:
            raise RuntimeError("Set SEC_USER_AGENT to a descriptive app name and contact email before accessing EDGAR")
        for attempt in range(3):
            with self._lock:
                now = time.monotonic()
                delay = self.min_interval - (now - self._last_call)
                if delay > 0:
                    time.sleep(delay)
                self._last_call = time.monotonic()

            request = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/html, */*" if not binary else "*/*",
            })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    if encoding == "gzip":
                        body = gzip.decompress(body)
                    elif encoding == "deflate":
                        import zlib
                        body = zlib.decompress(body)
                    return body
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or 500 <= exc.code < 600:
                    wait = min(8.0, float(exc.headers.get("Retry-After", 2 ** attempt)))
                else:
                    raise RuntimeError(f"SEC request failed ({exc.code}) for {urllib.parse.urlparse(url).path}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"SEC network request failed: {exc}") from exc
                wait = min(8.0, 2 ** attempt)
            if attempt < 2:
                time.sleep(wait)
        raise RuntimeError("SEC request failed after 3 attempts")

    def json(self, url: str) -> dict[str, Any]:
        return json.loads(self._fetch(url).decode("utf-8"))

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.json(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json")

    def filings(self, cik: str, forms: tuple[str, ...] = ("10-K", "10-Q"),
                fiscal_year: int | None = None) -> list[dict[str, Any]]:
        raw = self.submissions(cik)
        recent = raw.get("filings", {}).get("recent", {})
        def field_at(key: str, index: int, default: str = "") -> str:
            values = recent.get(key)
            if not isinstance(values, list) or index >= len(values):
                return default
            return values[index] or default

        found = []
        accessions = recent.get("accessionNumber", [])
        if not isinstance(accessions, list):
            accessions = []
        for i, accession in enumerate(accessions):
            if not accession:
                continue
            form = field_at("form", i)
            if form not in forms:
                continue
            filed = field_at("filingDate", i)
            report_date = field_at("reportDate", i)
            selected_date = report_date or filed
            if fiscal_year:
                try:
                    selected_year = int(selected_date[:4])
                except (TypeError, ValueError):
                    continue
                if selected_year not in (fiscal_year, fiscal_year + 1):
                    continue
            acc_path = accession.replace("-", "")
            primary = field_at("primaryDocument", i)
            archive = f"{SEC_ARCHIVES}/{int(cik)}/{acc_path}"
            found.append({"cik": str(int(cik)), "accession": accession, "form": form,
                "filed": filed, "period": report_date,
                "fiscal_period": field_at("fp", i), "fiscal_year": field_at("fy", i),
                "primary_document": primary, "url": f"{archive}/{primary}" if primary else archive,
                "index_url": f"{archive}/index.json"})
        return found

    def facts_for_year(self, cik: str, fiscal_year: int, raw: dict | None = None) -> dict[str, dict]:
        """Return annual reported facts, preferring USD but preserving a disclosed currency unit."""
        raw = raw or self.companyfacts(cik)
        concepts = raw.get("facts", {}).get("us-gaap", {})
        choices = {
            "revenue": [("RevenueFromContractWithCustomerExcludingAssessedTax", "currency"), ("Revenues", "currency"), ("SalesRevenueNet", "currency")],
            "gross_profit": [("GrossProfit", "currency")],
            "operating_income": [("OperatingIncomeLoss", "currency")],
            "net_income": [("NetIncomeLoss", "currency")],
            "eps_diluted": [("EarningsPerShareDiluted", "per_share")],
            "operating_cash_flow": [("NetCashProvidedByUsedInOperatingActivities", "currency")],
            "capital_expenditures": [("PaymentsToAcquirePropertyPlantAndEquipment", "currency")],
            "total_assets": [("Assets", "currency")],
            "total_liabilities": [("Liabilities", "currency")],
            "cash_and_equivalents": [("CashAndCashEquivalentsAtCarryingValue", "currency")],
            "total_debt": [("LongTermDebtAndFinanceLeaseObligations", "currency"), ("LongTermDebt", "currency")],
            "research_and_development": [("ResearchAndDevelopmentExpense", "currency")],
        }
        output: dict[str, dict] = {}
        for metric, options in choices.items():
            selected = None
            for tag, requested_unit in options:
                units = concepts.get(tag, {}).get("units", {})
                units_to_try = list(units) if requested_unit in {"currency", "per_share"} else [requested_unit]
                if requested_unit == "currency":
                    units_to_try = [unit for unit in units_to_try if re.fullmatch(r"[A-Z]{3}", unit)]
                    units_to_try.sort(key=lambda unit: (unit != "USD", unit))
                elif requested_unit == "per_share":
                    units_to_try = [unit for unit in units_to_try if re.fullmatch(r"[A-Z]{3}\s*/\s*shares?", unit, re.I)]
                    units_to_try.sort(key=lambda unit: (not unit.strip().upper().startswith("USD"), unit))
                for unit in units_to_try:
                    annual = [row for row in units.get(unit, []) if row.get("form") == "10-K" and
                              row.get("fy") == fiscal_year and row.get("fp") == "FY"]
                    if not annual:
                        continue
                    annual.sort(key=lambda row: (row.get("end", ""), row.get("filed", "")), reverse=True)
                    row = annual[0]
                    selected = {"metric": metric, "value": row.get("val"), "unit": unit,
                        "tag": tag, "start": row.get("start"), "end": row.get("end"), "filed": row.get("filed"),
                        "accession": row.get("acc"), "fy": fiscal_year, "form": row.get("form")}
                    break
                if selected:
                    break
            if selected:
                output[metric] = selected
        if "total_debt" not in output:
            parts = []
            for tag in ("LongTermDebtCurrent", "LongTermDebtNoncurrent"):
                unit_map = concepts.get(tag, {}).get("units", {})
                currencies = [unit for unit in unit_map if re.fullmatch(r"[A-Z]{3}", unit)]
                currencies.sort(key=lambda unit: (unit != "USD", unit))
                for unit in currencies:
                    annual = [row for row in unit_map[unit] if row.get("form") == "10-K" and row.get("fy") == fiscal_year and row.get("fp") == "FY"]
                    if annual:
                        annual.sort(key=lambda row: (row.get("end", ""), row.get("filed", "")), reverse=True)
                        row = dict(annual[0]); row["tag"] = tag; row["unit"] = unit
                        parts.append(row)
                        break
            if len(parts) == 2 and parts[0]["unit"] == parts[1]["unit"]:
                output["total_debt"] = {"metric": "total_debt", "value": sum(p["val"] for p in parts),
                    "unit": parts[0]["unit"], "tag": "+".join(p["tag"] for p in parts),
                    "end": parts[0].get("end"), "filed": parts[0].get("filed"),
                    "accession": parts[0].get("acc"), "fy": fiscal_year, "form": "10-K",
                    "calculation": "Current and non-current long-term debt summed where both components were available"}
        return output

    def download(self, url: str) -> bytes:
        return self._fetch(url, binary=True)

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self.json(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

