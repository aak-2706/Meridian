from __future__ import annotations

import json
import os
import re
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .agents import FinancialAgent, RiskAgent, StrategyAgent, SynthesizerAgent
from .config import settings
from .documents import ExtractedBlock, content_hash, extract_document
from .embeddings import EmbeddingClient
from .fx import FXRates
from .registry import Company
from .sec import SEC_ARCHIVES, SEC_DATA
from .store import Store, utcnow
from .transcripts import TranscriptClient
from .xbrl_review import compare_dimensioned_periods, extract_inline_xbrl_review, reconcile_fact_versions


def _period(year: int) -> str:
    return f"FY{year}"


def _has_cached_filing_coverage(store: Store, company_cik: str,
                               years: list[int], forms: tuple[str, ...]) -> bool:
    """Return true when every requested SEC form/year already has a stored filing."""
    if not years or not forms:
        return False
    documents = store.list_documents([company_cik])
    return all(any(document.get("document_type") == form and
                       str(year) in str(document.get("fiscal_period", ""))
                       for document in documents)
               for year in years for form in forms)


def _record(store: Store, run_id: str, stage: str, agent: str | None, started: str,
            output=None, error=None, token_usage=None, metadata=None, input_data=None):
    store.trace(run_id, stage, agent, "failed" if error else "completed", started,
                utcnow(), input_data=input_data, output_data=output, error=str(error) if error else None,
                token_usage=token_usage, metadata=metadata)


def _chunks_by_id(chunks: list[dict]) -> dict[str, dict]:
    out = {}
    for row in chunks:
        evidence = {"evidence_id": row["chunk_id"], "chunk_id": row["chunk_id"],
            "company": row["company_name"], "company_name": row["company_name"],
            "company_cik": row["company_cik"], "document_type": row["document_type"],
            "fiscal_period": row["fiscal_period"], "document_title": row.get("title", ""),
            "source_url": row.get("source_url"), "source_document_id": row["doc_id"],
            "page_number": row.get("page_number"), "section": row.get("section"),
            "table_id": row.get("table_id"), "content": row["content"],
            "content_type": row["content_type"], "extraction_method": row["extraction_method"],
            "retrieval_score": row.get("score")}
        out[evidence["evidence_id"]] = evidence
    return out


def ingest_upload(store: Store, company: Company, filename: str, payload: bytes,
                  document_type: str, fiscal_period: str, title: str = "",
                  source_url: str | None = None, content_type: str = "") -> dict:
    if not filename or len(payload) == 0:
        raise ValueError("An uploaded file and filename are required")
    allowed = {"10-K", "10-Q", "annual_report", "transcript", "other"}
    if document_type not in allowed:
        raise ValueError(f"Unsupported document type: {document_type}")
    extension = re.sub(r"[^a-z0-9.]", "", filename.lower().rsplit(".", 1)[-1])
    if extension not in {"pdf", "html", "htm", "xhtml", "txt"}:
        raise ValueError("Supported upload types are PDF, HTML, and plain text")
    if source_url:
        parsed_url = urlparse(source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Source URL must be an http or https URL")
    blocks = extract_document(filename, payload, content_type)
    digest = content_hash(payload)
    archive_dir = settings.upload_dir / "originals"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{digest}.{extension}"
    if not archive_path.exists():
        temp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
        temp_path.write_bytes(payload)
        os.replace(temp_path, archive_path)
    metadata = {"uploaded": True, "original_archived": True, "content_type": content_type}
    if document_type in {"10-K", "annual_report"} and extension in {"html", "htm", "xhtml"}:
        metadata["inline_xbrl_review"] = extract_inline_xbrl_review(payload)
    doc_id, created = store.add_document(company.as_dict(), document_type, fiscal_period,
        title or filename, source_url, digest, blocks, filename=filename,
        metadata=metadata)
    if not created and "inline_xbrl_review" in metadata:
        store.update_document_metadata(doc_id, {"inline_xbrl_review": metadata["inline_xbrl_review"]})
    return {"document_id": doc_id, "created": created, "block_count": len(blocks),
            "extraction_methods": sorted({b.extraction_method for b in blocks}),
            "original_archived": True}


def _ingest_sec_filing(store: Store, sec, company: Company, filing: dict) -> dict:
    payload = sec.download(filing["url"])
    blocks = extract_document(filing.get("primary_document", "filing.html"), payload,
                              "text/html")
    report_date = filing.get("period", "")
    year = filing.get("fiscal_year") or (report_date[:4] if report_date else filing.get("filed", "")[:4])
    fp = filing.get("fiscal_period")
    period = f"FY{year}" if fp == "FY" else f"{fp or filing['form']} {year}"
    inline_review = extract_inline_xbrl_review(payload) if filing.get("form") in {"10-K", "10-K/A"} else None
    doc_id, created = store.add_document(company.as_dict(), filing["form"], period,
        f"{company.company_name} {filing['form']} ({period})", filing["url"],
        content_hash(payload), blocks, accession=filing.get("accession", ""),
        filename=filing.get("primary_document", ""), metadata={"filed": filing.get("filed"),
        "report_date": report_date, "index_url": filing.get("index_url"),
        **({"inline_xbrl_review": inline_review} if inline_review is not None else {})})
    if not created and inline_review is not None:
        store.update_document_metadata(doc_id, {"inline_xbrl_review": inline_review})
    return {"document_id": doc_id, "created": created, "period": period,
            "form": filing["form"], "url": filing["url"], "block_count": len(blocks),
            "inline_xbrl_custom_tag_candidates": (inline_review or {}).get("custom_tags", {}).get("count", 0),
            "inline_xbrl_dimensioned_candidates": (inline_review or {}).get("dimensioned_facts", {}).get("count", 0)}


def _ingest_xbrl(store: Store, sec, company: Company, year: int, raw_facts: dict,
                 filing_by_accession: dict[str, dict]) -> dict:
    facts = sec.facts_for_year(company.cik, year, raw_facts)
    if not facts:
        return {"period": _period(year), "available": False, "metrics": 0,
                "restatement_candidates": []}
    restatement_candidates = reconcile_fact_versions(raw_facts, facts, year, filing_by_accession)
    rates = FXRates(settings.fx_rates_csv)
    blocks = []
    for metric, fact in facts.items():
        fact["metric"] = metric
        if fact.get("value") is not None:
            normalized = rates.normalize(float(fact["value"]), str(fact.get("unit", "")), year)
            fact.update(normalized)
        accession = str(fact.get("accession") or "")
        source_filing = filing_by_accession.get(accession, {}) if accession else {}
        fact["source_url"] = source_filing.get("url") or (
            f"{SEC_ARCHIVES}/{int(company.cik)}/{accession.replace('-', '')}/index.json"
            if accession else f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(company.cik):010d}.json")
        fact["extraction_method"] = "SEC companyfacts XBRL API"
        fact["table_id"] = fact.get("tag")
        fact["evidence_id"] = ""
        fact["formula"] = fact.get("calculation") or f"Reported SEC XBRL tag: {fact.get('tag')}"
        if fact.get("fx_rate") not in (None, 1.0):
            fact["formula"] += f"; annual-average FX to USD at {fact['fx_rate']:g}, source: {fact.get('fx_source', 'not supplied')}"
        reported = f"reported {fact.get('reported_value', fact['value'])} {fact.get('reported_unit', fact['unit'])}"
        normalized_text = f" | normalized {fact['value']} {fact['unit']} at FX {fact['fx_rate']} ({fact.get('fx_source')})" if fact.get("fx_rate") not in (None, 1.0) else ""
        content = (f"{metric} = {reported}{normalized_text} | fiscal period FY{year} | "
                   f"reporting date {fact.get('end') or 'unavailable'} | "
                   f"SEC tag {fact.get('tag')} | accession {accession or 'unavailable'} | {fact['formula']}")
        blocks.append(ExtractedBlock(content, "table", None, "Consolidated financial statements",
                                     fact.get("tag"), "SEC XBRL companyfacts"))
    company_payload = company.as_dict()
    source_urls = [f.get("source_url") for f in facts.values() if f.get("source_url")]
    digest = content_hash(json.dumps(facts, sort_keys=True).encode())
    doc_id, created = store.add_document(company_payload, "XBRL", _period(year),
        f"{company.company_name} SEC XBRL facts FY{year}",
        source_urls[0] if source_urls else f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(company.cik):010d}.json",
        digest, blocks, metadata={"source": "SEC companyfacts API", "fiscal_year": year,
            "facts": facts, "restatement_candidates": restatement_candidates})
    chunks = store.chunks_for(company.cik, "XBRL", [_period(year)])
    ids_by_tag = {row.get("table_id"): row["chunk_id"] for row in chunks if row["doc_id"] == doc_id}
    for metric, fact in facts.items():
        fact["evidence_ids"] = [ids_by_tag[fact["tag"]]] if fact.get("tag") in ids_by_tag else []
    store.update_document_metadata(doc_id, {"facts": facts, "restatement_candidates": restatement_candidates})
    return {"period": _period(year), "available": True, "metrics": len(facts),
            "created": created, "facts": facts, "document_id": doc_id,
            "restatement_candidates": restatement_candidates}


def run_research(run_id: str, request: dict, registry, store: Store, sec) -> None:
    """Run acquisition -> extraction/indexing -> specialist agents -> synthesis -> validation."""
    store.update_run(run_id, "running")
    try:
        started = utcnow()
        ciks = [str(int(x)) for x in request.get("company_ciks", [])]
        if len(ciks) != 2 or ciks[0] == ciks[1]:
            raise ValueError("Select two different companies")
        companies = [registry.by_cik(cik) for cik in ciks]
        if any(c is None for c in companies):
            raise ValueError("A selected company is not in the configured Fortune 500 universe")
        years = request.get("years") or [request.get("fiscal_year")]
        years = sorted({int(y) for y in years if y is not None})
        if not years or min(years) < 1990 or max(years) > datetime.now(timezone.utc).year + 1:
            raise ValueError("Select one or more valid fiscal years")
        periods = [_period(y) for y in years]
        forms = tuple(request.get("document_types") or ["10-K"])
        known_forms = {"10-K", "10-Q"}
        sec_forms = tuple(form for form in forms if form in known_forms)
        fast_mode = bool(request.get("fast_mode"))
        fetch_sec = False if fast_mode else request.get("fetch_sec", True)
        request = {**request, "company_ciks": ciks, "years": years,
                   "document_types": list(forms), "fetch_sec": fetch_sec,
                   "fast_mode": fast_mode}
        store.update_run_request(run_id, request)
        _record(store, run_id, "company_resolution", None, started,
            {"companies": [c.as_dict() for c in companies], "periods": periods},
            input_data={"selected_ciks": ciks, "selected_years": years})

        acquired = []
        acquisition_errors = []
        if request.get("fetch_sec", True) and sec_forms:
            def _acquire_company(company: Company):
                co_acquired = []
                co_errors = []
                stage_started = utcnow()
                if _has_cached_filing_coverage(store, company.cik, years, sec_forms):
                    cached_docs = [d for d in store.list_documents([company.cik])
                                   if d.get("document_type") in sec_forms and
                                   any(str(year) in str(d.get("fiscal_period", "")) for year in years)]
                    co_acquired.extend({"company": company.company_name, "cached": True,
                        "created": False, "document_id": d["doc_id"],
                        "period": d.get("fiscal_period"), "form": d.get("document_type"),
                        "url": d.get("source_url")} for d in cached_docs)
                    store.trace(run_id, "document_acquisition", None, "reused", stage_started,
                        utcnow(), output_data={"company": company.company_name,
                            "filings_found": len(cached_docs),
                            "note": "All requested filing periods are already stored; SEC discovery and downloads were skipped."})
                    return co_acquired, co_errors
                try:
                    filings = sec.filings(company.cik, sec_forms or ("10-K",), None)
                    selected = []
                    for filing in filings:
                        report_date = filing.get("period", "")
                        fiscal_year = filing.get("fiscal_year")
                        year = int(fiscal_year) if fiscal_year and str(fiscal_year).isdigit() else (int(report_date[:4]) if report_date else int(filing.get("filed", "0")[:4] or 0))
                        if year in years or (year - 1 in years and filing.get("fiscal_period") == "FY"):
                            selected.append(filing)
                    # Keep the most relevant annual report and all available quarters per selected year.
                    unique = {}
                    for filing in selected:
                        key = (filing["form"], filing.get("period"), filing.get("fiscal_period"))
                        unique[key] = filing
                    for filing in unique.values():
                        if store.has_accession(company.cik, filing.get("accession", "")):
                            stored = store.document_by_accession(company.cik, filing.get("accession", ""))
                            metadata = (stored or {}).get("metadata", {})
                            if filing.get("form") in {"10-K", "10-K/A"} and "inline_xbrl_review" not in metadata:
                                try:
                                    _ingest_sec_filing(store, sec, company, filing)
                                except (RuntimeError, ValueError) as exc:
                                    co_errors.append({"company": company.company_name,
                                        "stage": "inline_xbrl_review", "error": str(exc),
                                        "source_url": filing.get("url")})
                            co_acquired.append({"company": company.company_name,
                                "cached": True, "created": False, "period": filing.get("period"),
                                "form": filing["form"], "url": filing["url"]})
                            continue
                        try:
                            co_acquired.append({"company": company.company_name,
                                **_ingest_sec_filing(store, sec, company, filing)})
                        except (RuntimeError, ValueError) as exc:
                            co_errors.append({"company": company.company_name,
                                "stage": "filing_download_or_extraction", "error": str(exc),
                                "source_url": filing.get("url")})
                    _record(store, run_id, "document_acquisition", None, stage_started,
                            {"company": company.company_name, "filings_found": len(selected)})
                except (RuntimeError, ValueError) as exc:
                    co_errors.append({"company": company.company_name,
                        "stage": "filing_discovery", "error": str(exc)})
                    _record(store, run_id, "document_acquisition", None, stage_started,
                            error=str(exc), metadata={"company": company.company_name})
                return co_acquired, co_errors

            with ThreadPoolExecutor(max_workers=max(1, len(companies))) as executor:
                for co_acquired, co_errors in executor.map(_acquire_company, companies):
                    acquired.extend(co_acquired)
                    acquisition_errors.extend(co_errors)

        transcript_client = TranscriptClient()
        if "transcript" in forms and request.get("fetch_sec", True) and transcript_client.configured:
            for company in companies:
                for year in years:
                    stage_started = utcnow()
                    try:
                        transcript_rows = transcript_client.for_year(company.cik, company.company_name, year)
                        ingested = 0
                        for row in transcript_rows:
                            payload = row["text"].encode("utf-8")
                            blocks = extract_document("transcript.txt", payload, "text/plain")
                            doc_id, created = store.add_document(company.as_dict(), "transcript", row["period"],
                                row["title"], row.get("source_url"), content_hash(payload), blocks,
                                filename=row["title"] + ".txt", metadata={"transcript_provider": transcript_client.base_url})
                            if created:
                                ingested += 1
                            acquired.append({"company": company.company_name, "document_id": doc_id,
                                "created": created, "period": row["period"], "form": "transcript",
                                "url": row.get("source_url"), "block_count": len(blocks)})
                        _record(store, run_id, "transcript_acquisition", "Transcript Provider", stage_started,
                            {"company": company.company_name, "year": year,
                             "returned": len(transcript_rows), "new_documents": ingested})
                    except (RuntimeError, ValueError) as exc:
                        acquisition_errors.append({"company": company.company_name,
                            "stage": "transcript_acquisition", "error": str(exc)})
                        _record(store, run_id, "transcript_acquisition", "Transcript Provider", stage_started,
                            error=str(exc), metadata={"company": company.company_name, "year": year})
        elif "transcript" in forms:
            for company in companies:
                stage_started = utcnow()
                reason = "Automatic source discovery was disabled." if not request.get("fetch_sec", True) else "No transcript provider is configured."
                store.trace(run_id, "transcript_acquisition", "Transcript Provider", "skipped", stage_started,
                    utcnow(), output_data={"company": company.company_name, "note": reason})

        # SEC XBRL facts use the same source-specific accession and tag provenance as filings.
        facts_by_company: dict[str, dict] = {c.cik: {} for c in companies}
        if request.get("fetch_sec", True):
            def _fetch_xbrl_company(company: Company):
                stage_started = utcnow()
                cached = store.cached_xbrl_facts(company.cik, periods)
                facts_map = dict(cached)
                missing_years = [year for year in years if _period(year) not in cached]
                if not missing_years:
                    store.trace(run_id, "xbrl_ingestion", "Financial Agent", "reused",
                        stage_started, utcnow(), output_data={"company": company.company_name,
                            "periods": {period: len(facts) for period, facts in cached.items()},
                            "note": "All requested SEC XBRL facts are already cached."})
                    return company.cik, facts_map, []
                errs = []
                try:
                    raw_facts = sec.companyfacts(company.cik)
                    filing_map = {d.get("accession", ""): {"url": d.get("source_url"),
                        "form": d.get("document_type"), "period": d.get("fiscal_period")}
                        for d in store.list_documents([company.cik]) if d.get("accession")}
                    for year in missing_years:
                        xbrl = _ingest_xbrl(store, sec, company, year, raw_facts, filing_map)
                        if xbrl.get("available"):
                            facts_map[xbrl["period"]] = xbrl["facts"]
                    _record(store, run_id, "xbrl_ingestion", "Financial Agent", stage_started,
                        {"company": company.company_name,
                         "periods": {p: len(f) for p, f in facts_map.items()}})
                except (RuntimeError, ValueError) as exc:
                    errs.append({"company": company.company_name,
                        "stage": "xbrl_ingestion", "error": str(exc)})
                    _record(store, run_id, "xbrl_ingestion", "Financial Agent", stage_started,
                        error=str(exc), metadata={"company": company.company_name})
                return company.cik, facts_map, errs

            with ThreadPoolExecutor(max_workers=max(1, len(companies))) as executor:
                for cik, facts_map, errs in executor.map(_fetch_xbrl_company, companies):
                    facts_by_company[cik].update(facts_map)
                    acquisition_errors.extend(errs)
        else:
            for company in companies:
                stage_started = utcnow()
                facts_by_company[company.cik] = store.cached_xbrl_facts(company.cik, periods)
                status = "reused" if facts_by_company[company.cik] else "skipped"
                store.trace(run_id, "xbrl_ingestion", "Financial Agent", status, stage_started, utcnow(),
                    output_data={"company": company.company_name,
                        "periods": {period: len(facts) for period, facts in facts_by_company[company.cik].items()},
                        "note": "Loaded cached SEC XBRL values." if facts_by_company[company.cik] else "No cached SEC XBRL facts; automatic SEC access was disabled."})

        if not request.get("fetch_sec", True) or not sec_forms:
            for company in companies:
                trace_started = utcnow()
                reason = ("Automatic EDGAR discovery/download was disabled; using previously uploaded documents."
                          if not request.get("fetch_sec", True) else
                          "No SEC filing document type was selected; using previously stored documents.")
                store.trace(run_id, "document_acquisition", None, "skipped", trace_started, utcnow(),
                    output_data={"company": company.company_name, "note": reason})

        documents = store.list_documents(ciks)
        custom_tag_candidates = []
        dimensioned_candidates = []
        restatement_candidates = []
        seen_review_items: set[tuple] = set()
        selected_year_strings = {str(year) for year in years}
        for document in documents:
            if not any(year in document.get("fiscal_period", "") for year in selected_year_strings):
                continue
            stored = store.get_document(document["doc_id"])
            metadata = (stored or {}).get("metadata", {})
            inline_review = metadata.get("inline_xbrl_review", {})
            review_context = {"company": document.get("company_name", ""),
                "fiscal_period": document.get("fiscal_period", ""),
                "form": document.get("document_type", ""),
                "accession": document.get("accession", ""),
                "source_url": document.get("source_url", "")}
            for category, destination in (("custom_tags", custom_tag_candidates),
                                          ("dimensioned_facts", dimensioned_candidates)):
                for candidate in inline_review.get(category, {}).get("items", []):
                    key = (review_context["accession"], candidate.get("name"),
                           candidate.get("context_ref"), candidate.get("raw_value"))
                    if key in seen_review_items:
                        continue
                    seen_review_items.add(key)
                    destination.append({**candidate, **review_context})
            restatement_candidates.extend({**candidate,
                "company": document.get("company_name", ""),
                "source_document": document.get("source_url", "")}
                for candidate in metadata.get("restatement_candidates", []))

        segment_series = compare_dimensioned_periods(dimensioned_candidates)
        analyst_review = {
            "status": "analyst_review_required",
            "custom_tags": {"count": len(custom_tag_candidates), "items": custom_tag_candidates[:100],
                "truncated": max(0, len(custom_tag_candidates) - 100)},
            "dimensioned_facts": {"count": len(dimensioned_candidates), "items": dimensioned_candidates[:100],
                "truncated": max(0, len(dimensioned_candidates) - 100)},
            "segment_series": segment_series,
            "restatement_candidates": {"count": len(restatement_candidates),
                "items": restatement_candidates[:100], "truncated": max(0, len(restatement_candidates) - 100)},
            "notes": [
                "Issuer-specific tags and dimensioned facts are review candidates, not approved metric mappings or confirmed reportable segments.",
                "Changed facts across filings are possible restatements or reclassifications; review the original and later filing before deciding.",
                "No candidates can mean the source filing was unavailable, lacked Inline XBRL, or could not be parsed; it does not prove that custom tags or segments do not exist.",
            ],
        }
        docs_by_cik = {cik: [d for d in documents if d["company_cik"] == cik] for cik in ciks}
        for company in companies:
            company_docs = docs_by_cik[company.cik]
            new_count = sum(1 for item in acquired if item.get("company") == company.company_name and item.get("created"))
            cached_count = len(company_docs) - new_count
            trace_started = utcnow()
            status = "completed" if new_count else "reused" if company_docs else "failed"
            reason = None if company_docs else "No stored or retrieved document was available for this company."
            store.trace(run_id, "document_extraction", None, status, trace_started, utcnow(),
                output_data={"company": company.company_name, "new_documents_extracted": new_count,
                             "stored_documents_reused": max(0, cached_count),
                             "note": "Text, filing HTML, tables, and configured scanned-page OCR/vision extraction; method is retained per evidence block."},
                error=reason)
            trace_started = utcnow()
            store.trace(run_id, "indexing", None, status, trace_started, utcnow(),
                output_data={"company": company.company_name, "documents_indexed_or_reused": len(company_docs),
                             "vector_store": "SQLite", "embedding": EmbeddingClient().identity},
                error=reason)

        requested_doc_types = [x for x in forms if x in {"10-K", "10-Q", "transcript", "annual_report", "other"}]
        risk_queries = "risk factors litigation regulation cybersecurity supply chain competition customer concentration liquidity"
        strategy_queries = "management strategy capital expenditure research development product outlook guidance acquisition expansion artificial intelligence"
        # Pre-embed queries so all subsequent searches are instantaneous vector comparisons
        embedding_client = EmbeddingClient()
        embedding_client.embed_query(risk_queries)
        embedding_client.embed_query(strategy_queries)

        risk_results, strategy_results = [], []
        evidence: dict[str, dict] = {}
        analysis_jobs = []

        def _retrieve_company_evidence(company: Company):
            risk_started = utcnow()
            risk_evidence = store.search(risk_queries, [company.cik], periods, requested_doc_types,
                                         limit=store_limit(store))
            risk_evidence = _chunks_by_id(risk_evidence)
            _record(store, run_id, "evidence_retrieval", None, risk_started,
                {"company": company.company_name, "specialist": "risk",
                 "evidence_count": len(risk_evidence), "periods": periods,
                 "document_types": requested_doc_types},
                input_data={"query": risk_queries, "company_cik": company.cik})

            strategy_started = utcnow()
            strategy_evidence = store.search(strategy_queries, [company.cik], periods, requested_doc_types,
                                             limit=store_limit(store))
            strategy_evidence = _chunks_by_id(strategy_evidence)
            _record(store, run_id, "evidence_retrieval", None, strategy_started,
                {"company": company.company_name, "specialist": "strategy",
                 "evidence_count": len(strategy_evidence), "periods": periods,
                 "document_types": requested_doc_types},
                input_data={"query": strategy_queries, "company_cik": company.cik})
            return company, risk_evidence, strategy_evidence

        with ThreadPoolExecutor(max_workers=max(1, len(companies))) as executor:
            retrieval_results = list(executor.map(_retrieve_company_evidence, companies))

        for company, risk_evidence, strategy_evidence in retrieval_results:
            evidence.update(risk_evidence)
            evidence.update(strategy_evidence)
            risk_agent = RiskAgent()
            analysis_jobs.append({"kind": "risk", "company": company, "agent": risk_agent,
                "evidence": list(risk_evidence.values()), "evidence_ids": list(risk_evidence),
                "agent_started": utcnow()})
            strategy_agent = StrategyAgent()
            analysis_jobs.append({"kind": "strategy", "company": company, "agent": strategy_agent,
                "evidence": list(strategy_evidence.values()), "evidence_ids": list(strategy_evidence),
                "agent_started": utcnow()})

        # SEC retrieval and evidence filtering are complete; overlap the four
        # independent provider calls so Groq latency is not paid serially.
        analysis_outputs = {}
        with ThreadPoolExecutor(max_workers=max(1, len(analysis_jobs))) as executor:
            futures = {executor.submit(job["agent"].run, job["company"].company_name,
                                       job["evidence"]): index
                       for index, job in enumerate(analysis_jobs)}
            for future in as_completed(futures):
                index = futures[future]
                job = analysis_jobs[index]
                analysis = future.result()
                token_use = analysis.pop("token_usage", None)
                analysis_outputs[index] = analysis
                _record(store, run_id, f"{job['kind']}_analysis", job["agent"].name,
                    job["agent_started"], {"company": job["company"].company_name,
                        "findings": len(analysis.get("findings", [])),
                        "status": analysis.get("status")},
                    token_usage=token_use, metadata={"evidence_count": len(job["evidence"])},
                    input_data={"evidence_ids": job["evidence_ids"]})
        for index, job in enumerate(analysis_jobs):
            output = {"company": job["company"].company_name, **analysis_outputs[index]}
            (risk_results if job["kind"] == "risk" else strategy_results).append(output)

        for company in companies:
            for row in store.chunks_for(company.cik, "XBRL", periods):
                evidence.setdefault(row["chunk_id"], _chunks_by_id([row])[row["chunk_id"]])
        financial_started = utcnow()
        financial = FinancialAgent().run([c.as_dict() for c in companies], facts_by_company)
        financial["analyst_review"] = analyst_review
        _record(store, run_id, "financial_analysis", "Financial Agent", financial_started,
            {"metrics_by_company": {k: sum(len(v) for v in data["periods"].values()) for k, data in financial["metrics"].items()},
             "analyst_review_counts": {"custom_tags": analyst_review["custom_tags"]["count"],
                 "dimensioned_facts": analyst_review["dimensioned_facts"]["count"],
                 "segment_series": analyst_review["segment_series"]["count"],
                 "restatement_candidates": analyst_review["restatement_candidates"]["count"]}},
            input_data={"company_ciks": ciks, "periods": periods,
                "source": "SEC Company Facts annual US-GAAP facts"})

        coverage = {}
        for company in companies:
            docs = docs_by_cik[company.cik]
            present_periods = {d["fiscal_period"] for d in docs}
            present_types = {d["document_type"] for d in docs}
            requested_transcript = "transcript" in forms
            coverage[company.cik] = {"company": company.company_name,
                "documents": [{"title": d["title"], "type": d["document_type"],
                               "period": d["fiscal_period"], "source_url": d["source_url"]} for d in docs],
                "periods_with_documents": sorted(present_periods),
                "missing_periods": [p for p in periods if not any(p[2:] in x for x in present_periods)],
                "missing_document_types": [kind for kind in requested_doc_types if kind not in present_types],
                "transcript_available": any(d["document_type"] == "transcript" for d in docs),
                "transcript_note": ("No transcript service is configured; upload a transcript if required." if not transcript_client.configured else
                    "Automatic transcript discovery was disabled." if not request.get("fetch_sec", True) else
                    "The configured transcript service returned no transcript for the selected periods.")
                    if requested_transcript and not any(d["document_type"] == "transcript" for d in docs) else ""}
            limitations = []
            vision_ready = settings.vision_enabled and bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)
            ocr_ready = bool((shutil.which(settings.tesseract_cmd) or Path(settings.tesseract_cmd).is_file())
                             and (shutil.which(settings.pdftoppm_cmd) or Path(settings.pdftoppm_cmd).is_file()))
            if not vision_ready and not ocr_ready:
                limitations.append("Scanned PDF pages require configured Tesseract plus Poppler, or a vision-capable LLM endpoint.")
            if not transcript_client.configured and requested_transcript and not any(d["document_type"] == "transcript" for d in docs):
                limitations.append("No earnings-call transcript was available from an automatic provider or uploaded source.")
            if not EmbeddingClient().configured:
                limitations.append("Retrieval uses the local feature-hash and lexical hybrid; configure an embeddings endpoint for trained semantic vectors.")
            if not settings.fx_rates_csv.is_file():
                limitations.append("Non-USD XBRL amounts stay in reported currency unless annual FX rates are supplied; no silent currency conversion is applied.")
            coverage[company.cik]["processing_limitations"] = limitations

        synth_started = utcnow()
        memo = SynthesizerAgent().run([c.as_dict() for c in companies], risk_results,
            strategy_results, financial, periods, coverage, evidence)
        memo["financial"].setdefault("analyst_review", {})["claim_reviews"] = memo["validation"].get("claim_review_queue", {"count": 0, "items": []})
        _record(store, run_id, "synthesis", "Synthesizer", synth_started,
            {"supported_claims": memo["validation"]["claims_supported"],
             "removed_claims": memo["validation"]["claims_removed"]},
            input_data={"risk_agent_count": len(risk_results), "strategy_agent_count": len(strategy_results),
                "financial_company_count": len(financial.get("metrics", {}))})
        evidence_started = utcnow()
        _record(store, run_id, "evidence_validation", "Synthesizer", evidence_started,
            {**memo["validation"], "evidence_count": len(evidence),
             "companies_covered": sorted({e["company_cik"] for e in evidence.values()}),
             "retrieval_filters": {"company_ciks": ciks, "periods": periods, "document_types": requested_doc_types}},
            input_data={"claim_count": memo["validation"]["claims_checked"],
                "evidence_count": len(evidence)})

        if not evidence:
            missing = "; ".join([f"{c.company_name}: no documents or evidence available" for c in companies])
            raise RuntimeError("Insufficient evidence to generate a comparative memo. " + missing)
        coverage_gap = any(cov["missing_periods"] or cov["missing_document_types"] for cov in coverage.values())
        result = {"run_id": run_id, "status": "complete_with_gaps" if acquisition_errors or coverage_gap else "complete",
            "execution_mode": "fast_cached" if fast_mode else "live_or_cached",
            "memo": memo, "acquired_documents": acquired, "acquisition_errors": acquisition_errors,
            "created_at": utcnow(), "evaluation": {"claim_validation": memo["validation"],
                "context_precision": None, "context_recall": None, "hallucination_rate": None,
                "note": "Retrieval and unsupported-claim rates are available from the reproducible fixture evaluation; this run does not claim a benchmark score."}}
        store.update_run(run_id, result["status"], result)
    except Exception as exc:
        store.update_run(run_id, "failed", error=str(exc))
        try:
            _record(store, run_id, "pipeline", None, utcnow(), error=str(exc),
                    metadata={"traceback": traceback.format_exc(limit=4)})
        except Exception:
            pass


def store_limit(store: Store) -> int:
    return settings.retrieval_limit


def markdown_memo(result: dict) -> str:
    memo = result["memo"]
    companies = memo["companies"]
    rows = [f"# {memo['title']}", "", "## 1. Executive summary", "",
        memo.get("executive_summary", "Evidence-limited comparative review."), "",
        "## 2. Companies compared and comparison rationale", "",
        f"**{companies[0]} vs {companies[1]}.** {memo.get('comparison_reason', 'Pair selected by user.')}", "",
        "## 3. Research scope and data coverage", "",
        f"Selected reporting years: {', '.join(memo['periods'])}.", ""]
    for cov in memo["coverage"].values():
        rows.append(f"- **{cov['company']}**: {len(cov['documents'])} source document(s); missing periods: {', '.join(cov['missing_periods']) or 'none identified'}.")
        if cov["transcript_note"]:
            rows.append(f"  - {cov['transcript_note']}")
        if cov.get("missing_document_types"):
            rows.append(f"  - Missing requested document types: {', '.join(cov['missing_document_types'])}.")
        for limitation in cov.get("processing_limitations", []):
            rows.append(f"  - Processing limitation: {limitation}")
    if result.get("acquisition_errors"):
        rows.extend(["", "Acquisition errors:"])
        rows.extend([f"- {x['company']} / {x['stage']}: {x['error']}" for x in result["acquisition_errors"]])
    rows.extend(["", "## 4. Key financial comparison", "",
        "Values are reported annual SEC XBRL facts unless marked as calculated. Unavailable facts are omitted.", "",
        "| Company | Period | Metric | Value | Calculation or reported tag | Evidence |", "|---|---|---|---:|---|---|"])
    for cik, data in memo["financial"].get("metrics", {}).items():
        for year, metrics in data.get("periods", {}).items():
            for key, item in metrics.items():
                if item.get("value") is None:
                    continue
                value = item["value"]
                unit = item.get("unit", "")
                if unit == "%":
                    display = f"{value:.2f}%"
                elif "/" in unit:
                    display = f"{value:,.2f} {unit}"
                else:
                    display = f"{value:,.0f} {unit}".strip()
                refs = ", ".join(f"[{e}]" for e in item.get("evidence_ids", [])) or "Unavailable"
                formula = item.get("formula") or f"Reported XBRL tag `{item.get('tag', 'not available')}`"
                rows.append(f"| {data['company']} | {year} | {key.replace('_',' ').title()} | {display} | {formula} | {refs} |")
    analyst_review = memo.get("financial", {}).get("analyst_review", {})
    rows.extend(["", "## Analyst review queue", "",
        "These are machine-detected review candidates. They do not imply an accounting error or an approved interpretation.", ""])
    custom_tags = analyst_review.get("custom_tags", {})
    rows.append(f"- Issuer-specific tag candidates: {custom_tags.get('count', 0)}.")
    for item in custom_tags.get("items", [])[:25]:
        source = f" — {item['source_url']}" if item.get("source_url") else ""
        rows.append(f"  - {item.get('company', 'Company')} · `{item.get('name', 'unknown tag')}` = {item.get('raw_value', '')} {item.get('unit_ref', '')}; {item.get('fiscal_period', 'period unavailable')}; review definition and unit.{source}")
    dimensioned = analyst_review.get("dimensioned_facts", {})
    rows.append(f"- Dimensioned fact candidates: {dimensioned.get('count', 0)}; these may include segment dimensions and need review.")
    for item in dimensioned.get("items", [])[:25]:
        dimensions = ", ".join(f"{d.get('dimension', '?')}={d.get('member', '?')}" for d in item.get("dimensions", []))
        source = f" — {item['source_url']}" if item.get("source_url") else ""
        rows.append(f"  - {item.get('company', 'Company')} · `{item.get('name', 'unknown tag')}` = {item.get('raw_value', '')} {item.get('unit_ref', '')}; {dimensions}; period {item.get('period', {}).get('instant') or item.get('period', {}).get('end', 'unknown')}.{source}")
    series = analyst_review.get("segment_series", {})
    rows.append(f"- Same-tag/same-dimension period series: {series.get('count', 0)}; confirm the segment definitions and measurement basis before comparing.")
    for item in series.get("items", [])[:20]:
        values = "; ".join(f"{p.get('end')}: {p.get('value')} {item.get('unit_ref', '')}" for p in item.get("periods", []))
        rows.append(f"  - {item.get('tag', 'unknown tag')} · {item.get('dimensions', [])}: {values}")
    restatements = analyst_review.get("restatement_candidates", {})
    rows.append(f"- Changed same-period fact candidates: {restatements.get('count', 0)}; review original and later filing before calling these restatements.")
    for item in restatements.get("items", [])[:25]:
        old = item.get("as_filed", {})
        new = item.get("latest_comparative", {})
        rows.append(f"  - {item.get('company', 'Company')} · {item.get('metric', item.get('tag'))} FY{item.get('fiscal_year')}: {old.get('value')} {item.get('unit')} as filed {old.get('filed')} → {new.get('value')} {item.get('unit')} in filing {new.get('filed')}; possible restatement/reclassification.")
    claim_reviews = analyst_review.get("claim_reviews", {})
    rows.append(f"- Claims needing human entailment review: {claim_reviews.get('count', 0)}.")
    for item in claim_reviews.get("items", [])[:25]:
        rows.append(f"  - {item.get('company') or 'Company'} · `{item.get('support_status', 'review')}` · {item.get('claim', '')} [Evidence: {', '.join(item.get('evidence_ids', []))}]")
    rows.extend(["", "Review notes:"])
    rows.extend(f"- {note}" for note in analyst_review.get("notes", []))
    rows.extend(["", "## 5. Financial performance and trends", "",
        "Revenue growth and margins are calculated only when the required source facts and non-zero denominators are available. No values are interpolated.", "",
        "## 6. Liquidity, leverage, and cash flow", "",
        "Review cash and equivalents, reported debt and liabilities, operating cash flow, and calculated free cash flow in the table above. Debt totals are omitted when SEC facts do not supply a complete combined figure or both current and non-current components.", "",
        "## 7. Risk comparison", ""])
    for agent in memo["risks"]:
        rows.append(f"### {agent['company']} — {agent['agent']}")
        if not agent.get("findings"):
            rows.append("No evidence-backed findings were produced from the retrieved documents.")
        for finding in agent.get("findings", []):
            rows.append(f"- **{finding['category']} ({finding['severity']})** — {finding['description']} [Evidence: {', '.join(finding['evidence_ids'])}]")
            rows.append(f"  - Severity rationale: {finding.get('severity_rationale','')}")
            rows.append(f"  - Uncertainty: {finding.get('uncertainty','')}")
    rows.extend(["", "## 8. Strategic priorities and management outlook", ""])
    for agent in memo["strategies"]:
        rows.append(f"### {agent['company']} — {agent['agent']}")
        if not agent.get("findings"):
            rows.append("No evidence-backed strategic statement was identified.")
        for finding in agent.get("findings", []):
            rows.append(f"- **{finding['category']}** ({finding.get('statement_type','source excerpt')}) — {finding['description']} [Evidence: {', '.join(finding['evidence_ids'])}]")
            rows.append(f"  - Attribution and uncertainty: {finding.get('uncertainty','')}")
    rows.extend(["", "## 9. Material changes across reporting periods", "",
        "No risk trend is inferred from wording differences alone. The current automated comparison does not establish material changes unless comparable source passages support them; see each finding's period and uncertainty.", "",
        "## 10. Strengths, weaknesses, opportunities, and threats", "",
        memo.get("swot_assessment", "Not assessed; analyst review required."), "",
        "## 11. Important differences and similarities", ""])
    if memo.get("similarities"):
        rows.append("Evidence categories found for both companies:")
        for item in memo["similarities"]:
            rows.append(f"- **{item['category']}** — evidence IDs: {', '.join(item['evidence_ids'])}")
    else:
        rows.append("No shared risk or strategy category was identified in the retrieved evidence.")
    if memo.get("differences"):
        rows.append("Observed one-sided categories (absence in retrieved evidence does not establish absence of exposure):")
        for item in memo["differences"]:
            rows.append(f"- **{item['company']} · {item['category']}** — {item['interpretation']} [Evidence: {', '.join(item['evidence_ids'])}]")
    else:
        rows.append("No one-sided categories were identified in the retrieved evidence.")
    rows.extend(["", "## 12. Supporting evidence and source references", "",
        "Evidence IDs link memo findings and calculated metrics to the excerpts below.", "",
        "## 13. Data gaps, conflicts, and limitations", ""])
    for limitation in memo.get("limitations", []):
        rows.append(f"- {limitation}")
    rows.extend(["", "## Evidence validation", "",
        f"Checked {memo['validation']['claims_checked']} material claim(s); retained {memo['validation']['claims_supported']} supported claims and removed {memo['validation']['claims_removed']} unsupported claims.",
        memo['validation']['method'], ""])
    for cov in memo["coverage"].values():
        if cov.get("missing_periods"):
            rows.append(f"- **{cov['company']}** is missing documents for {', '.join(cov['missing_periods'])}.")
        if cov.get("transcript_note"):
            rows.append(f"- **{cov['company']}** transcript coverage: {cov['transcript_note']}")
    if result.get("acquisition_errors"):
        rows.extend(f"- **{x['company']}** {x['stage']}: {x['error']}" for x in result["acquisition_errors"])
    rows.extend(["", "## 14. Final comparative assessment", "",
        memo.get("comparative_assessment", "Review the cited facts and coverage limitations before drawing conclusions."), "",
        "No unsupported investment recommendation or forecast is provided.", "", "## Sources", ""])
    for item in memo["evidence"]:
        where = f"page {item['page_number']}" if item.get("page_number") else (f"table {item['table_id']}" if item.get("table_id") else "location unavailable")
        source = item.get("source_url") or "uploaded document"
        rows.append(f"- `{item['evidence_id']}` — {item['company']}, {item['document_type']} {item['fiscal_period']}, {item['document_title']}, {where}. {source}")
    return "\n".join(rows) + "\n"
