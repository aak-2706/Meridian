from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any


_STANDARD_TAXONOMY_PREFIXES = {
    "country", "currency", "dei", "exch", "ifrs-full", "iso4217", "link",
    "naics", "srt", "us-gaap", "utr", "xbrldi", "xbrli", "xlink", "ix",
}


def _local_name(tag: str) -> str:
    return tag.rsplit(":", 1)[-1].casefold()


def _number(text: str, scale: str, sign: str) -> float | None:
    cleaned = re.sub(r"[\s,$€£¥]", "", text)
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned)
        if scale:
            value *= Decimal(10) ** int(scale)
        if sign == "-" or negative:
            value = -abs(value)
        return float(value)
    except (InvalidOperation, ValueError, OverflowError):
        return None


class _InlineFactParser(HTMLParser):
    """Extract review candidates from Inline XBRL without assigning accounting meaning."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.namespaces: dict[str, str] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, Any]] = []
        self.context: dict[str, Any] | None = None
        self.context_capture: dict[str, Any] | None = None
        self.fact: dict[str, Any] | None = None
        self.fact_excluded = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        values = {str(k).casefold(): (v or "") for k, v in attrs}
        for key, value in values.items():
            if key.startswith("xmlns:"):
                self.namespaces[key.split(":", 1)[1]] = value

        local = _local_name(tag)
        if local == "context" and values.get("id"):
            self.context = {"id": values["id"], "start": "", "end": "", "instant": "", "dimensions": []}
            self.contexts[values["id"]] = self.context
        elif self.context is not None and local in {"instant", "startdate", "enddate"}:
            self.context_capture = {"kind": "date", "name": local, "parts": []}
        elif self.context is not None and local in {"explicitmember", "typedmember"}:
            dimension = values.get("dimension", "")
            row = {"dimension": dimension, "member": "", "typed": local == "typedmember"}
            self.context["dimensions"].append(row)
            self.context_capture = {"kind": "dimension", "row": row, "parts": []}

        if self.fact is not None and local == "exclude":
            self.fact_excluded += 1
        elif self.fact is None and local in {"nonfraction", "nonnumeric"} and values.get("name"):
            self.fact = {
                "inline_type": local,
                "name": values["name"],
                "context_ref": values.get("contextref", ""),
                "unit_ref": values.get("unitref", ""),
                "scale": values.get("scale", "0"),
                "sign": values.get("sign", ""),
                "continued_at": values.get("continuedat", ""),
                "parts": [],
            }

    def handle_endtag(self, tag: str):
        local = _local_name(tag)
        if self.fact is not None and local == "exclude" and self.fact_excluded:
            self.fact_excluded -= 1
        elif self.fact is not None and local == self.fact["inline_type"]:
            text = re.sub(r"\s+", " ", " ".join(self.fact.pop("parts"))).strip()
            name = self.fact["name"]
            prefix = name.split(":", 1)[0] if ":" in name else ""
            context = self.contexts.get(self.fact["context_ref"], {})
            fact = {**self.fact, "raw_value": text, "prefix": prefix,
                    "taxonomy_uri": self.namespaces.get(prefix, ""),
                    "period": {key: context.get(key, "") for key in ("start", "end", "instant")},
                    "dimensions": list(context.get("dimensions", []))}
            if fact["inline_type"] == "nonfraction":
                fact["value"] = _number(text, fact.get("scale", "0"), fact.get("sign", ""))
            else:
                fact["value"] = None
            self.facts.append(fact)
            self.fact = None
            self.fact_excluded = 0
        elif self.context_capture is not None and local in {"instant", "startdate", "enddate", "explicitmember", "typedmember"}:
            value = re.sub(r"\s+", " ", " ".join(self.context_capture["parts"])).strip()
            if self.context_capture["kind"] == "date" and self.context is not None:
                key = {"startdate": "start", "enddate": "end", "instant": "instant"}[self.context_capture["name"]]
                self.context[key] = value
            elif self.context_capture["kind"] == "dimension":
                self.context_capture["row"]["member"] = value
            self.context_capture = None
        elif local == "context":
            self.context = None
            self.context_capture = None

    def handle_data(self, data: str):
        if self.fact is not None:
            if not self.fact_excluded:
                self.fact["parts"].append(data)
        elif self.context_capture is not None:
            self.context_capture["parts"].append(data)


def extract_inline_xbrl_review(payload: bytes, limit: int = 100) -> dict[str, Any]:
    """Return custom-tag and dimensional fact candidates for human review.

    This intentionally does not map issuer tags to financial metrics and does not
    assume every dimensional fact represents a reportable segment.
    """
    parser = _InlineFactParser()
    try:
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, AssertionError):
        return {"available": False, "custom_tags": {"count": 0, "items": []},
                "dimensioned_facts": {"count": 0, "items": []},
                "note": "Inline XBRL could not be parsed; analyst review was not automated."}

    custom = []
    dimensioned = []
    seen_custom: set[tuple] = set()
    seen_dimensioned: set[tuple] = set()
    for fact in parser.facts:
        prefix = fact.get("prefix", "").casefold()
        if prefix and prefix not in _STANDARD_TAXONOMY_PREFIXES:
            key = (fact["name"], fact["context_ref"], fact["raw_value"], fact["unit_ref"])
            if key not in seen_custom:
                seen_custom.add(key)
                custom.append({**fact, "review_status": "pending_analyst_review",
                    "review_reason": "Issuer-specific XBRL concept; confirm its definition, unit, period, and relationship to standard metrics."})
        if fact.get("dimensions"):
            key = (fact["name"], fact["context_ref"], fact["raw_value"], fact["unit_ref"])
            if key not in seen_dimensioned:
                seen_dimensioned.add(key)
                dimensioned.append({**fact, "review_status": "pending_analyst_review",
                    "review_reason": "Dimensioned XBRL fact; confirm whether the dimensions describe reportable segments and whether periods are comparable."})

    return {
        "available": bool(parser.facts),
        "custom_tags": {"count": len(custom), "items": custom[:limit], "truncated": max(0, len(custom) - limit)},
        "dimensioned_facts": {"count": len(dimensioned), "items": dimensioned[:limit], "truncated": max(0, len(dimensioned) - limit)},
        "note": "Candidates are extracted from Inline XBRL. The software does not approve tag mappings or infer segment comparability.",
    }


def reconcile_fact_versions(raw_facts: dict, selected_facts: dict[str, dict], fiscal_year: int,
                            filing_by_accession: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """Find same-tag/unit/period annual values that changed in later 10-K filings.

    A difference is only a possible restatement or reclassification. It is not
    automatically classified as a restatement.
    """
    filing_by_accession = filing_by_accession or {}
    taxonomies = raw_facts.get("facts", {}) if isinstance(raw_facts, dict) else {}
    candidates: list[dict[str, Any]] = []
    for metric, selected in selected_facts.items():
        tag, unit, end = selected.get("tag"), selected.get("unit"), selected.get("end")
        if not tag or not unit or not end:
            continue
        concepts = next((taxonomy.get(tag) for taxonomy in taxonomies.values()
                         if isinstance(taxonomy, dict) and tag in taxonomy), None)
        rows = (concepts or {}).get("units", {}).get(unit, [])
        matches = [row for row in rows if row.get("form") in {"10-K", "10-K/A"} and row.get("end") == end]
        if selected.get("start"):
            matches = [row for row in matches if row.get("start") == selected.get("start")]
        else:
            matches = [row for row in matches if not row.get("start")]
        versions: dict[str, dict] = {}
        for row in matches:
            accession = row.get("acc") or f"{row.get('filed','')}:{row.get('form','')}"
            prior = versions.get(accession)
            if prior is None or row.get("filed", "") >= prior.get("filed", ""):
                filing = filing_by_accession.get(row.get("acc", ""), {})
                versions[accession] = {"value": row.get("val"), "filed": row.get("filed", ""),
                    "form": row.get("form", ""), "fiscal_year": row.get("fy"),
                    "accession": row.get("acc", ""), "source_url": filing.get("url", "")}
        ordered = sorted(versions.values(), key=lambda row: (row.get("filed", ""), row.get("accession", "")))
        if len(ordered) < 2 or len({json_value(row.get("value")) for row in ordered}) < 2:
            continue
        baseline = next((row for row in ordered if row.get("fiscal_year") == fiscal_year), ordered[0])
        latest = ordered[-1]
        if json_value(baseline.get("value")) == json_value(latest.get("value")):
            continue
        old_value, new_value = baseline.get("value"), latest.get("value")
        try:
            delta = float(new_value) - float(old_value)
            percent = delta / float(old_value) * 100 if float(old_value) else None
        except (TypeError, ValueError, ZeroDivisionError):
            delta, percent = None, None
        candidates.append({"metric": metric, "tag": tag, "unit": unit, "period_end": end,
            "fiscal_year": fiscal_year, "as_filed": baseline, "latest_comparative": latest,
            "delta": delta, "delta_percent": percent, "versions": ordered,
            "review_status": "possible_restatement_or_reclassification",
            "review_reason": "The same XBRL tag, unit, and period has different values across 10-K filings. Review the filings before treating this as a restatement."})
    return candidates


def compare_dimensioned_periods(facts: list[dict[str, Any]], limit: int = 100) -> dict[str, Any]:
    """Line up identical XBRL dimensions across periods; comparability stays pending."""
    groups: dict[tuple, dict[str, Any]] = {}
    for fact in facts:
        period = fact.get("period", {})
        end = period.get("instant") or period.get("end")
        if not end or fact.get("value") is None or not fact.get("dimensions"):
            continue
        dimensions = tuple(sorted((row.get("dimension", ""), row.get("member", ""), bool(row.get("typed")))
                                  for row in fact["dimensions"]))
        key = (fact.get("name", ""), fact.get("unit_ref", ""), dimensions)
        group = groups.setdefault(key, {"tag": fact.get("name", ""), "unit_ref": fact.get("unit_ref", ""),
            "dimensions": list(fact["dimensions"]), "periods": {}})
        period_key = f"{period.get('start') or 'instant'}..{end}"
        group["periods"][period_key] = {"start": period.get("start", ""), "end": end,
            "value": fact["value"], "raw_value": fact.get("raw_value", ""),
            "source_url": fact.get("source_url", ""), "accession": fact.get("accession", "")}

    series = []
    for group in groups.values():
        periods = [group["periods"][key] for key in sorted(group["periods"])]
        if len(periods) < 2:
            continue
        series.append({**group, "periods": periods, "review_status": "pending_analyst_review",
            "review_reason": "The same XBRL tag and dimensions appear in multiple periods. Confirm segment identity, definition, and measurement basis before comparing."})
    series.sort(key=lambda row: (row["tag"], str(row["dimensions"])))
    return {"count": len(series), "items": series[:limit], "truncated": max(0, len(series) - limit)}


def json_value(value: Any) -> str:
    return str(value)
