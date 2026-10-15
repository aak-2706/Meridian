from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Company:
    company_name: str
    ticker: str = ""
    cik: str = ""
    rank: int | None = None
    reporting_year: int | None = None
    industry: str = ""
    aliases: tuple[str, ...] = ()
    source_url: str = ""

    def as_dict(self) -> dict:
        out = asdict(self)
        out["aliases"] = list(self.aliases)
        out["research_ready"] = bool(self.cik)
        out["availability_reason"] = "" if self.cik else "No SEC CIK is mapped; this row is searchable but cannot be used for SEC research yet."
        return out


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


class CompanyRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.companies = self._load()

    def _load(self) -> list[Company]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            out = []
            for row in rows:
                name = (row.get("company_name") or "").strip()
                if not name:
                    continue
                try:
                    rank = int(row["rank"]) if row.get("rank", "").strip() else None
                    year = int(row["reporting_year"]) if row.get("reporting_year", "").strip() else None
                except ValueError as exc:
                    raise ValueError(f"Invalid rank or reporting_year for {name}") from exc
                aliases = tuple(x.strip() for x in (row.get("aliases") or "").split("|") if x.strip())
                cik = re.sub(r"\D", "", row.get("cik") or "")
                out.append(Company(name, (row.get("ticker") or "").strip().upper(), cik,
                                   rank, year, (row.get("industry") or "").strip(), aliases,
                                   (row.get("source_url") or "").strip()))
            return out

    @property
    def source(self) -> dict:
        years = sorted({c.reporting_year for c in self.companies if c.reporting_year})
        sources = sorted({c.source_url for c in self.companies if c.source_url})
        ready = sum(bool(c.cik) for c in self.companies)
        return {"count": len(self.companies), "research_ready_count": ready,
                "unresolved_count": len(self.companies) - ready, "reporting_years": years,
                "sources": sources, "refreshable": True}

    def search(self, query: str = "", limit: int = 100) -> list[Company]:
        q = _key(query)
        matches = [c for c in self.companies if not q or q in _key(" ".join(
            [c.company_name, c.ticker, c.cik, *c.aliases]))]
        return sorted(matches, key=lambda c: (c.rank or 9999, c.company_name.casefold()))[:limit]

    def resolve(self, query: str) -> Company:
        q = _key(query)
        matches = [c for c in self.companies if q in {_key(c.company_name), _key(c.ticker),
                  _key(c.cik), *(_key(a) for a in c.aliases)}]
        if len(matches) != 1:
            raise ValueError("Company is ambiguous or is not in the configured Fortune 500 universe")
        return matches[0]

    def by_cik(self, cik: str) -> Company | None:
        target = str(int(cik)) if str(cik).isdigit() else str(cik)
        return next((c for c in self.companies if c.cik == target), None)

