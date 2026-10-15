from __future__ import annotations

import json
from pathlib import Path
import re

from .store import _vector


def _similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _local_hybrid_score(query: str, passage: str) -> float:
    semantic = max(0.0, _similarity(_vector(query), _vector(passage)))
    query_terms = set(re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)?", query.lower()))
    passage_terms = set(re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)?", passage.lower()))
    lexical = len(query_terms & passage_terms) / (len(query_terms) or 1)
    return semantic * 0.75 + lexical * 0.25


def run_fixture_evaluation(path: Path) -> dict:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    case_results = []
    precision_values, recall_values = [], []
    claim_total = 0
    unsupported = 0
    for case in dataset["cases"]:
        ranked = sorted(case["passages"], key=lambda p: _local_hybrid_score(case["query"], p["content"]), reverse=True)
        retrieved = ranked[:case.get("k", 3)]
        expected = set(case["expected_evidence_ids"])
        retrieved_ids = {p["id"] for p in retrieved}
        relevant_retrieved = retrieved_ids & expected
        precision = len(relevant_retrieved) / len(retrieved) if retrieved else 0.0
        recall = len(relevant_retrieved) / len(expected) if expected else None
        precision_values.append(precision)
        if recall is not None:
            recall_values.append(recall)
        claims = case.get("claims", [])
        unsupported_ids = []
        for claim in claims:
            claim_total += 1
            source = next((p for p in case["passages"] if p["id"] == claim.get("evidence_id")), None)
            supported = bool(source and claim.get("quote") and claim["quote"].casefold() in source["content"].casefold())
            if not supported:
                unsupported += 1
                unsupported_ids.append(claim.get("id", "unknown"))
        case_results.append({"case_id": case["id"], "retrieved_ids": [p["id"] for p in retrieved],
            "expected_evidence_ids": sorted(expected), "context_precision": precision,
            "context_recall": recall, "claims_evaluated": len(claims),
            "unsupported_claims": unsupported_ids})
    return {"dataset_name": dataset["name"], "dataset_size": len(dataset["cases"]),
        "composition": dataset.get("composition", {}),
        "methodology": {"context_precision": "Per case, relevant retrieved passage IDs divided by retrieved passage count; macro average.",
            "context_recall": "Per case, relevant retrieved passage IDs divided by reference evidence IDs; macro average. Empty references are excluded.",
            "hallucination_rate": "Unsupported or contradicted claim count divided by evaluated claims. Support is an exact cited-quote substring check; this is a deterministic proxy, not an independent semantic judge."},
        "context_precision": sum(precision_values) / len(precision_values) if precision_values else None,
        "context_recall": sum(recall_values) / len(recall_values) if recall_values else None,
        "hallucination_rate": unsupported / claim_total if claim_total else None,
        "claims_evaluated": claim_total, "unsupported_claims": unsupported,
        "cases": case_results,
        "limitations": ["Small deterministic fixture set; it does not represent live SEC retrieval quality.",
                        "Exact quote checks miss paraphrase support and do not replace human or independent model review."]}
