from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meridian.config import settings


SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def _column(headers: list[str] | None, *names: str) -> str | None:
    by_key = {_header_key(header): header for header in (headers or [])}
    for name in names:
        if _header_key(name) in by_key:
            return by_key[_header_key(name)]
    return None


def _ticker_key(value: str) -> str:
    return re.sub(r"[./]", "-", (value or "").strip().upper())


def _fetch_sec_ciks() -> dict[str, str]:
    user_agent = settings.sec_user_agent.strip()
    if not user_agent or "contact@example.com" in user_agent.casefold():
        raise ValueError("Set SEC_USER_AGENT in the project .env with a real monitored contact email before resolving CIKs from SEC data.")
    request = urllib.request.Request(SEC_TICKER_MAP_URL, headers={
        "User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                body = zlib.decompress(body)
            payload = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError, zlib.error) as exc:
        raise RuntimeError(f"Could not retrieve SEC's ticker-to-CIK mapping: {exc}") from exc
    fields = payload.get("fields", [])
    if not {"cik", "ticker"}.issubset(fields):
        raise ValueError("SEC ticker mapping response did not contain its expected cik and ticker fields")
    cik_index, ticker_index = fields.index("cik"), fields.index("ticker")
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in payload.get("data", []):
        if len(row) <= max(cik_index, ticker_index):
            continue
        ticker, cik = _ticker_key(str(row[ticker_index] or "")), str(row[cik_index] or "")
        if not ticker or not cik:
            continue
        if ticker in mapping and mapping[ticker] != cik:
            ambiguous.add(ticker)
        else:
            mapping[ticker] = str(int(cik))
    for ticker in ambiguous:
        mapping.pop(ticker, None)
    return mapping


def _existing_ciks() -> dict[str, str]:
    path = ROOT / "data" / "fortune500.csv"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        ticker_col = _column(reader.fieldnames, "ticker")
        cik_col = _column(reader.fieldnames, "cik")
        if not ticker_col or not cik_col:
            return {}
        return {_ticker_key(row.get(ticker_col, "")): re.sub(r"\D", "", row.get(cik_col, ""))
                for row in reader if row.get(ticker_col) and row.get(cik_col)}


def import_universe(source: Path, output: Path, year: int, source_url: str = "",
                    resolve_ciks: bool = False) -> tuple[int, int, list[int]]:
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        company_col = _column(reader.fieldnames, "company_name", "company", "name")
        ticker_col = _column(reader.fieldnames, "ticker", "symbol")
        cik_col = _column(reader.fieldnames, "cik", "central_index_key")
        rank_col = _column(reader.fieldnames, "rank", "fortune_rank")
        industry_col = _column(reader.fieldnames, "industry", "sector")
        aliases_col = _column(reader.fieldnames, "aliases", "alias")
        source_col = _column(reader.fieldnames, "source_url", "source")
        missing = [name for name, column in (("company/name", company_col), ("ticker", ticker_col), ("rank", rank_col)) if not column]
        if missing:
            raise ValueError("Missing recognizable CSV column(s): " + ", ".join(missing))
        rows = list(reader)
    if not 2 <= len(rows) <= 500:
        raise ValueError("Expected between 2 and 500 company rows")
    sec_ciks = _fetch_sec_ciks() if resolve_ciks else {}
    known_ciks = _existing_ciks()
    seen_cik, seen_rank, tied_ranks = set(), set(), set()
    normalized = []
    for row in rows:
        name = (row.get(company_col) or "").strip()
        ticker = (row.get(ticker_col) or "").strip().upper()
        cik_raw = re.sub(r"\D", "", row.get(cik_col) or "") if cik_col else ""
        if not cik_raw and ticker:
            cik_raw = sec_ciks.get(_ticker_key(ticker), "") or known_ciks.get(_ticker_key(ticker), "")
        try:
            rank = int(float(row.get(rank_col) or ""))
        except ValueError as exc:
            raise ValueError(f"Invalid rank for {name or '(unnamed company)'}") from exc
        if not name:
            raise ValueError("Every row needs a company name")
        if cik_raw and not 1 <= int(cik_raw) <= 9_999_999_999:
            raise ValueError(f"Company {name} has an invalid CIK")
        if rank < 1 or rank > 500:
            raise ValueError(f"Rank must be between 1 and 500 for {name}")
        if cik_raw and cik_raw in seen_cik:
            raise ValueError(f"Duplicate CIK: {cik_raw}")
        if rank in seen_rank:
            tied_ranks.add(rank)
        if cik_raw:
            seen_cik.add(cik_raw)
        seen_rank.add(rank)
        normalized.append({"company_name": name, "ticker": ticker, "cik": str(int(cik_raw)) if cik_raw else "",
            "rank": rank, "reporting_year": year,
            "industry": (row.get(industry_col) or "").strip() if industry_col else "",
            "aliases": (row.get(aliases_col) or "").strip() if aliases_col else "",
            "source_url": source_url or ((row.get(source_col) or "").strip() if source_col else "")})
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["company_name", "ticker", "cik", "rank", "reporting_year", "industry", "aliases", "source_url"])
        writer.writeheader(); writer.writerows(normalized)
    temp.replace(output)
    return len(normalized), len(seen_cik), sorted(tied_ranks)


def main():
    parser = argparse.ArgumentParser(description="Validate and import a Fortune 500 company CSV")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "fortune500.csv")
    parser.add_argument("--resolve-ciks", action="store_true",
        help="Resolve missing ticker CIKs from the SEC's public ticker mapping; requires SEC_USER_AGENT in .env")
    args = parser.parse_args()
    count, research_ready, tied_ranks = import_universe(args.csv, args.output, args.year, args.source_url, args.resolve_ciks)
    print(f"Imported {count} company rows for ranking year {args.year} into {args.output}")
    print(f"{research_ready} rows have a CIK and are SEC research-ready; unresolved rows remain searchable but cannot run SEC research until mapped.")
    if tied_ranks:
        print("Tied rank(s) preserved from the source: " + ", ".join(map(str, tied_ranks)))


if __name__ == "__main__":
    main()
