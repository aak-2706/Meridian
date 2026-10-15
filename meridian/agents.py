from __future__ import annotations

import re

from .llm import LLMClient
from .config import settings


RISK_TERMS = {
    "Legal and regulatory": ("litigation", "legal proceeding", "regulatory", "investigation", "compliance"),
    "Cybersecurity and privacy": ("cybersecurity", "cyber attack", "data breach", "privacy incident"),
    "Operations and supply chain": ("supply chain", "supplier concentration", "manufacturing disruption", "operational disruption"),
    "Competition and market": ("competition", "competitive", "market share", "pricing pressure"),
    "Macroeconomic and geopolitical": ("geopolitical", "inflation", "foreign currency", "macroeconomic", "tariff"),
    "Customer and counterparty concentration": ("customer concentration", "single customer", "counterparty concentration"),
    "Financial and liquidity": ("liquidity risk", "debt covenant", "going concern", "credit risk"),
}
STRATEGY_TERMS = {
    "Capital investment": ("capital expenditure", "capital expenditures", "capital investment", "capacity expansion"),
    "Research and development": ("research and development", "r&d", "research and development expense"),
    "Products and markets": ("product launch", "new product", "roadmap", "market expansion", "launching"),
    "Technology and AI": ("artificial intelligence", "generative ai", "machine learning", "cloud infrastructure"),
    "M&A and portfolio": ("acquisition", "acquire", "divestiture", "strategic investment"),
    "Management outlook": ("outlook", "guidance", "expect to", "we plan to", "strategic priorities"),
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|(?<=\|)\s*", text) if len(s.strip()) >= 35]


def _finding_evidence(evidence: list[dict], categories: dict[str, tuple[str, ...]], maximum: int = 7) -> list[dict]:
    found = []
    used = set()
    for category, terms in categories.items():
        candidates = []
        for item in evidence:
            content = item.get("content", "")
            lower = content.casefold()
            hits = [term for term in terms if term in lower]
            if not hits:
                continue
            sentence = max(_sentences(content), key=lambda s: sum(t in s.casefold() for t in terms), default=content[:500])
            sentence = sentence[:700]
            score = len(hits) * 3 + float(item.get("score", 0))
            candidates.append((score, item, sentence, hits))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, item, sentence, hits in candidates[:1]:
            if item["chunk_id"] in used:
                continue
            used.add(item["chunk_id"])
            words = {w.lower() for w in re.findall(r"[a-z]+", sentence)}
            severity = "high" if any(x in sentence.casefold() for x in ("material weakness", "significant risk", "substantial doubt", "data breach", "major litigation")) else "moderate" if len(hits) > 1 else "watch"
            finding = {"category": category, "company": item["company_name"],
                "description": sentence, "severity": severity,
                "severity_rationale": "Keyword severity heuristic; analyst review is required.",
                "period": item["fiscal_period"], "evidence_ids": [item["chunk_id"]],
                "quote": sentence, "confidence": "medium" if len(hits) > 1 else "low",
                "uncertainty": "Automated term matching does not establish likelihood, magnitude, or trend.",
                "change_over_periods": "Not assessed without comparable passages from multiple periods."}
            if item.get("document_type") == "transcript":
                finding["statement_type"] = "Earnings-call transcript excerpt"
            elif any(x in (item.get("section") or "").casefold() for x in ("management discussion", "item 7")):
                finding["statement_type"] = "Management discussion disclosure excerpt"
            else:
                finding["statement_type"] = "Filing disclosure excerpt; management attribution is not confirmed by rule-based analysis"
            found.append(finding)
            if len(found) >= maximum:
                return found
    return found


def _valid_model_findings(output: dict, evidence: list[dict], allowed_categories: dict[str, tuple[str, ...]]) -> list[dict]:
    by_id = {row["chunk_id"]: row for row in evidence}
    accepted = []
    for item in output.get("findings", []) if isinstance(output, dict) else []:
        ids = item.get("evidence_ids", []) if isinstance(item, dict) else []
        quote = str(item.get("quote", "")).strip()
        if not ids or not quote:
            continue
        source = by_id.get(ids[0])
        if not source or quote.casefold() not in source["content"].casefold():
            continue
        category = str(item.get("category", "")).strip()
        if category not in allowed_categories:
            continue
        accepted.append({"category": category, "company": source["company_name"],
            "description": quote, "severity": item.get("severity") if item.get("severity") in {"high", "moderate", "watch"} else "watch",
            "severity_rationale": "Model assessment; verify against the cited filing.",
            "period": source["fiscal_period"], "evidence_ids": [source["chunk_id"]],
            "quote": quote, "confidence": "model-assessed",
            "statement_type": "Earnings-call transcript excerpt" if source.get("document_type") == "transcript" else "Filing excerpt",
            "uncertainty": str(item.get("uncertainty", "Model interpretation; analyst review required."))[:500],
            "change_over_periods": "Not inferred unless explicitly evidenced across comparable periods."})
    return accepted


class RiskAgent:
    name = "Risk Agent"
    system_prompt = """You are Meridian's risk-disclosure analyst. Identify material risks explicitly disclosed in the supplied evidence. Distinguish boilerplate from evidence of a concrete exposure. Do not infer increasing risk from wording changes alone. Return JSON {findings:[{category, severity, quote, evidence_ids, uncertainty}]}. Quote an exact substring from one cited passage. Use only supplied IDs. No investment advice."""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()

    def run(self, company: str, evidence: list[dict]) -> dict:
        result = _finding_evidence(evidence, RISK_TERMS)
        tokens = None
        if self.llm.configured and evidence:
            payload = {"company": company, "evidence": [{"id": e["chunk_id"], "period": e["fiscal_period"],
                "section": e.get("section"), "content": e["content"][:1800]} for e in evidence]}
            try:
                response, tokens = self.llm.complete_json(self.system_prompt, payload, timeout=12.0)
                model_items = _valid_model_findings(response, evidence, RISK_TERMS)
                if model_items:
                    result = model_items
            except RuntimeError as exc:
                return {"agent": self.name, "findings": result, "status": "fallback",
                        "note": str(exc), "token_usage": tokens}
        return {"agent": self.name, "findings": result, "status": "completed" if evidence else "insufficient_evidence",
                "note": "" if evidence else "No matching source evidence was retrieved.", "token_usage": tokens}


class StrategyAgent:
    name = "Strategy Agent"
    system_prompt = """You are Meridian's strategy-disclosure analyst. Extract management's explicit plans and outlook from the supplied passages. Separate disclosed statements from interpretation, and never claim transcript analysis unless a transcript is in the evidence. Return JSON {findings:[{category, quote, evidence_ids, uncertainty}]}. Quote an exact substring from one cited passage. Use only supplied IDs. No investment advice."""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()

    def run(self, company: str, evidence: list[dict]) -> dict:
        result = _finding_evidence(evidence, STRATEGY_TERMS)
        tokens = None
        if self.llm.configured and evidence:
            payload = {"company": company, "evidence": [{"id": e["chunk_id"], "period": e["fiscal_period"],
                "document_type": e["document_type"], "section": e.get("section"), "content": e["content"][:1800]} for e in evidence]}
            try:
                response, tokens = self.llm.complete_json(self.system_prompt, payload, timeout=12.0)
                model_items = _valid_model_findings(response, evidence, STRATEGY_TERMS)
                if model_items:
                    result = model_items
            except RuntimeError as exc:
                return {"agent": self.name, "findings": result, "status": "fallback",
                        "note": str(exc), "token_usage": tokens}
        return {"agent": self.name, "findings": result, "status": "completed" if evidence else "insufficient_evidence",
                "note": "" if evidence else "No matching source evidence was retrieved.", "token_usage": tokens}


METRIC_LABELS = {
    "revenue": "Revenue", "gross_profit": "Gross profit", "operating_income": "Operating income",
    "net_income": "Net income", "eps_diluted": "Diluted EPS", "operating_cash_flow": "Operating cash flow",
    "capital_expenditures": "Capital expenditures", "total_assets": "Total assets",
    "total_liabilities": "Total liabilities", "cash_and_equivalents": "Cash and equivalents",
    "total_debt": "Total debt", "research_and_development": "Research and development",
}


class FinancialAgent:
    name = "Financial Agent"

    def run(self, companies: list[dict], facts_by_company: dict[str, dict]) -> dict:
        metrics = {}
        for company in companies:
            cik = company["cik"]
            rows = facts_by_company.get(cik, {})
            metrics[cik] = {"company": company["company_name"], "periods": rows}
            for year, items in rows.items():
                revenue = items.get("revenue", {}).get("value")
                gross = items.get("gross_profit", {}).get("value")
                op = items.get("operating_income", {}).get("value")
                net = items.get("net_income", {}).get("value")
                if revenue not in (None, 0):
                    margin_inputs = (("gross_margin", "gross_profit", gross),
                        ("operating_margin", "operating_income", op), ("net_margin", "net_income", net))
                    for key, numerator_key, numerator in margin_inputs:
                        numerator_unit = items.get(numerator_key, {}).get("unit")
                        revenue_unit = items.get("revenue", {}).get("unit")
                        if numerator is not None and (not numerator_unit or not revenue_unit or numerator_unit == revenue_unit):
                            items[key] = {"value": numerator / revenue * 100, "unit": "%",
                                "formula": f"{METRIC_LABELS[numerator_key]} / Revenue × 100",
                                "evidence_ids": list(set(items.get(numerator_key, {}).get("evidence_ids", []) + items["revenue"].get("evidence_ids", [])))}
            years = sorted(rows)
            for older, newer in zip(years, years[1:]):
                old_rev = rows[older].get("revenue", {}).get("value")
                new_rev = rows[newer].get("revenue", {}).get("value")
                old_unit = rows[older].get("revenue", {}).get("unit")
                new_unit = rows[newer].get("revenue", {}).get("unit")
                if old_rev not in (None, 0) and new_rev is not None and (not old_unit or not new_unit or old_unit == new_unit):
                    rows[newer]["revenue_growth"] = {"value": (new_rev - old_rev) / old_rev * 100,
                        "unit": "%", "formula": f"(Revenue {newer} - Revenue {older}) / Revenue {older} × 100",
                        "evidence_ids": list(set(rows[newer]["revenue"].get("evidence_ids", []) + rows[older]["revenue"].get("evidence_ids", [])))}
            for year, items in rows.items():
                ocf = items.get("operating_cash_flow", {}).get("value")
                capex = items.get("capital_expenditures", {}).get("value")
                ocf_unit = items.get("operating_cash_flow", {}).get("unit")
                capex_unit = items.get("capital_expenditures", {}).get("unit")
                if ocf is not None and capex is not None and (not ocf_unit or not capex_unit or ocf_unit == capex_unit):
                    items["free_cash_flow"] = {"value": ocf - abs(capex), "unit": ocf_unit or "",
                        "formula": "Operating cash flow - absolute capital expenditures",
                        "evidence_ids": list(set(items["operating_cash_flow"].get("evidence_ids", []) + items["capital_expenditures"].get("evidence_ids", [])))}
        return {"agent": self.name, "status": "completed" if any(facts_by_company.values()) else "insufficient_evidence",
                "metrics": metrics, "note": "SEC XBRL reported facts; missing facts remain unavailable."}


class SynthesizerAgent:
    name = "Synthesizer"

    def run(self, companies: list[dict], risks: list[dict], strategies: list[dict], financial: dict,
            periods: list[str], coverage: dict, evidence: dict[str, dict]) -> dict:
        names = [c["company_name"] for c in companies]
        factual_claims = []
        for agent in risks + strategies:
            for finding in agent.get("findings", []):
                factual_claims.append({"claim": finding["description"], "company": agent.get("company", ""),
                    "evidence_ids": finding["evidence_ids"], "quote": finding.get("quote", ""),
                    "source_type": "disclosure"})
        for cik_data in financial.get("metrics", {}).values():
            for year, items in cik_data.get("periods", {}).items():
                for label, item in items.items():
                    if isinstance(item, dict) and item.get("value") is not None:
                        factual_claims.append({"claim": f"{cik_data['company']} {year} {label}: {item['value']:.4g} {item.get('unit','')}",
                            "evidence_ids": item.get("evidence_ids", []), "quote": "", "source_type": "metric",
                            "source_value": item.get("reported_value", item.get("value")),
                            "source_unit": item.get("reported_unit", item.get("unit")),
                            "calculated": item.get("reported_value") is None})
        validated = []
        claim_review_queue = []
        judged = 0
        judge_unavailable = 0
        judge_rejected = 0
        semantically_supported = 0
        judge_client = LLMClient()
        use_judge = settings.claim_judge_enabled and judge_client.configured
        review_candidates = {}
        for index, claim in enumerate(factual_claims):
            if claim.get("source_type") != "disclosure":
                continue
            ids = [item_id for item_id in claim["evidence_ids"] if item_id in evidence]
            matching = [evidence[item_id] for item_id in ids
                        if claim.get("quote") and claim["quote"].casefold() in evidence[item_id]["content"].casefold()]
            if matching:
                review_candidates[index] = (ids, matching)

        # One bounded provider call replaces one network round-trip per claim.
        # Overflow stays visible in the analyst queue instead of delaying the memo.
        judge_batch_size = 24
        judge_ids = list(review_candidates)[:judge_batch_size]
        judge_results = {}
        judge_error = None
        if use_judge and judge_ids:
            judge_payload = [{"id": str(index), "claim": factual_claims[index]["claim"],
                "quoted_text": factual_claims[index]["quote"],
                "cited_passages": [source["content"][:2400] for source in review_candidates[index][1]]}
                for index in judge_ids]
            judge_prompt = "Assess whether each cited financial disclosure entails the whole claim. Be conservative: a verbatim quote may still fail to support a broader claim. For every input id return one result with assessment supported, partly_supported, contradicted, or unclear, plus a short reason. Use supported only when the cited excerpt directly supports every material part. Return JSON {assessments:[{id,assessment,reason}]} and do not omit ids."
            try:
                response, _ = judge_client.complete_json(judge_prompt,
                    {"claims": judge_payload}, timeout=20.0)
                for result in response.get("assessments", []) if isinstance(response, dict) else []:
                    if not isinstance(result, dict):
                        continue
                    try:
                        index = int(result.get("id"))
                    except (TypeError, ValueError):
                        continue
                    assessment = str(result.get("assessment", "")).casefold()
                    if assessment not in {"supported", "partly_supported", "contradicted", "unclear"}:
                        assessment = "unclear"
                    if index in judge_ids:
                        judge_results[index] = {"assessment": assessment,
                            "reason": str(result.get("reason", ""))[:500]}
            except RuntimeError as exc:
                judge_error = str(exc)

        for index, claim in enumerate(factual_claims):
            ids = [i for i in claim["evidence_ids"] if i in evidence]
            if not ids:
                continue
            if claim["source_type"] == "disclosure":
                sources = [evidence[item_id] for item_id in ids]
                matching = [source for source in sources if claim["quote"].casefold() in source["content"].casefold()]
                if not claim["quote"] or not matching:
                    continue
                claim["support_status"] = "exact_quote_only"
                if use_judge:
                    if index in judge_results:
                        judged += 1
                        decision = judge_results[index]
                        assessment = decision["assessment"]
                        claim["support_status"] = assessment
                        claim["support_reason"] = decision["reason"]
                        if assessment != "supported":
                            judge_rejected += 1
                            claim_review_queue.append({"claim": claim["claim"], "company": claim.get("company", ""),
                                "evidence_ids": ids, "quote": claim["quote"], "support_status": assessment,
                                "reason": claim["support_reason"] or "The semantic judge did not confirm full support."})
                            continue
                        semantically_supported += 1
                    elif index not in judge_ids:
                        claim["support_status"] = "requires_analyst_review"
                        claim_review_queue.append({"claim": claim["claim"], "company": claim.get("company", ""),
                            "evidence_ids": ids, "quote": claim["quote"],
                            "support_status": "requires_analyst_review",
                            "reason": "This claim exceeded the one-request semantic-review limit and remains queued for analyst review."})
                    else:
                        judge_unavailable += 1
                        claim["support_status"] = "requires_analyst_review"
                        claim_review_queue.append({"claim": claim["claim"], "company": claim.get("company", ""),
                            "evidence_ids": ids, "quote": claim["quote"],
                            "support_status": "requires_analyst_review",
                            "reason": ("The configured semantic judge was unavailable; exact quotation matching does not establish entailment. " + judge_error)
                                if judge_error else "The semantic judge omitted this claim; exact quotation matching does not establish entailment."})
                else:
                    claim["support_status"] = "requires_analyst_review"
                    claim_review_queue.append({"claim": claim["claim"], "company": claim.get("company", ""),
                        "evidence_ids": ids, "quote": claim["quote"],
                        "support_status": "requires_analyst_review",
                        "reason": "No semantic judge is configured; exact quotation matching does not establish entailment."})
            elif claim.get("source_value") is not None and not claim.get("calculated"):
                needle = format(float(claim["source_value"]), ".12g")
                contents = " ".join(evidence[item_id]["content"] for item_id in ids)
                if needle not in contents:
                    continue
                claim["support_status"] = "reported_value_match"
            elif claim.get("calculated"):
                claim["support_status"] = "calculation_with_cited_inputs"
            claim["evidence_ids"] = ids
            validated.append(claim)
        risk_by_company = {x["company"]: x.get("findings", []) for x in risks}
        strategy_by_company = {x["company"]: x.get("findings", []) for x in strategies}
        overlap_risks = set.intersection(*(set(f["category"] for f in risk_by_company.get(n, [])) for n in names)) if names else set()
        overlap_strategies = set.intersection(*(set(f["category"] for f in strategy_by_company.get(n, [])) for n in names)) if names else set()
        similarities = []
        for category in sorted(overlap_risks | overlap_strategies):
            supporting = [f for company_name in names
                for f in risk_by_company.get(company_name, []) + strategy_by_company.get(company_name, [])
                if f["category"] == category]
            similarities.append({"category": category,
                "evidence_ids": sorted({eid for f in supporting for eid in f["evidence_ids"]})})
        differences = []
        for agent_group in (risks, strategies):
            for agent in agent_group:
                other = next((x for x in agent_group if x["company"] != agent["company"]), {})
                other_categories = {f["category"] for f in other.get("findings", [])}
                for finding in agent.get("findings", []):
                    if finding["category"] not in other_categories:
                        differences.append({"company": agent["company"], "category": finding["category"],
                            "evidence_ids": finding["evidence_ids"], "interpretation": "Observed in retrieved evidence for this company; absence in the other company's retrieved passages does not establish absence of the underlying exposure."})
        return {"title": f"Comparative Equity Research: {names[0]} vs {names[1]}",
            "companies": names, "periods": periods,
            "comparison_reason": "The company pair was selected by the user; no separate comparison rationale was supplied.",
            "executive_summary": f"This evidence-limited review covers {names[0]} and {names[1]} across {', '.join(periods)}. The system retained {len(validated)} claim(s) after evidence checks and removed {len(factual_claims) - len(validated)} unsupported claim(s). Claims without semantic review are flagged for an analyst. The results describe reported disclosures and metrics only; they are not an investment recommendation.",
            "similarities": similarities, "differences": differences,
            "comparative_assessment": "The available material supports a comparison of the reported facts and cited disclosures shown below. Missing documents, differing fiscal periods, and XBRL tag availability limit broader conclusions; no relative investment rating is assigned.",
            "swot_assessment": "Not assigned automatically. The available evidence and heuristic analyses do not support a complete, balanced SWOT assessment without analyst review.",
            "coverage": coverage, "risks": risks, "strategies": strategies,
            "financial": financial, "claims": validated,
            "validation": {"claims_checked": len(factual_claims), "claims_supported": len(validated),
                "claims_removed": len(factual_claims) - len(validated),
                "method": "Disclosure claims require a cited exact quote and evidence ID. Exact-quote matching is only a citation check. A separately configured LLM judge classifies entailment probabilistically in one bounded batch; claims without a judgment and non-supported judgments are listed for analyst review. Reported numeric facts must match cited source text; calculated metrics retain their input evidence IDs.",
                "semantic_judge_enabled": use_judge, "semantic_claims_judged": judged,
                "semantic_claims_supported": semantically_supported,
                "semantic_judge_unavailable": judge_unavailable, "semantic_claims_rejected": judge_rejected,
                "semantic_claims_skipped_for_latency": max(0, len(review_candidates) - len(judge_ids)) if use_judge else 0,
                "claims_requiring_analyst_review": len(claim_review_queue),
                "claim_review_queue": {"count": len(claim_review_queue), "items": claim_review_queue[:100],
                    "truncated": max(0, len(claim_review_queue) - 100)}},
            "limitations": ["Custom tags, dimensioned facts, candidate segment comparisons, and changed facts across filings are surfaced for analyst review; the system does not automatically approve their interpretation.",
                "The support judge is probabilistic when enabled. Without it, disclosure support uses exact quotation matching and is explicitly flagged for analyst review."],
            "evidence": list(evidence.values())}

