from __future__ import annotations

import csv
import re
from pathlib import Path


class FXRates:
    """Annual-average FX table. Each rate is USD per one unit of from_currency."""

    def __init__(self, path: Path):
        self.rates: dict[tuple[int, str], dict] = {}
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"year", "from_currency", "to_currency", "rate", "source"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError("FX CSV headers must include year,from_currency,to_currency,rate,source")
            for row in reader:
                year = int(row["year"])
                source_currency = row["from_currency"].strip().upper()
                target = row["to_currency"].strip().upper()
                rate = float(row["rate"])
                if not 1990 <= year <= 2100 or not re.fullmatch(r"[A-Z]{3}", source_currency) or target != "USD" or rate <= 0:
                    raise ValueError(f"Invalid FX rate row for year {year}")
                self.rates[(year, source_currency)] = {"rate": rate, "source": row["source"].strip(),
                    "rate_date": (row.get("rate_date") or "").strip()}

    @staticmethod
    def currency(unit: str) -> str | None:
        match = re.match(r"^\s*([A-Z]{3})(?:\s*/\s*shares?)?\s*$", unit or "", re.I)
        return match.group(1).upper() if match else None

    def normalize(self, value: float, unit: str, year: int) -> dict:
        currency = self.currency(unit)
        if not currency:
            return {"value": value, "unit": unit, "reported_value": value, "reported_unit": unit}
        if currency == "USD":
            return {"value": value, "unit": unit, "reported_value": value, "reported_unit": unit,
                    "fx_rate": 1.0, "fx_source": "identity"}
        rate = self.rates.get((year, currency))
        if not rate:
            return {"value": value, "unit": unit, "reported_value": value, "reported_unit": unit}
        normalized_unit = "USD / shares" if "/" in unit else "USD"
        return {"value": value * rate["rate"], "unit": normalized_unit,
            "reported_value": value, "reported_unit": unit, "fx_rate": rate["rate"],
            "fx_source": rate["source"], "fx_rate_date": rate["rate_date"],
            "formula": f"Reported {value:g} {unit} converted at annual average {rate['rate']:g} USD per {currency}; source: {rate['source']}"}
