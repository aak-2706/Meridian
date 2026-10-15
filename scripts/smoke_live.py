"""Read-only connectivity checks for explicitly configured live data providers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meridian.config import settings
from meridian.embeddings import EmbeddingClient
from meridian.llm import LLMClient
from meridian.sec import SecClient
from meridian.transcripts import TranscriptClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", default="320193", help="SEC CIK for a company in the configured universe")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--providers", action="store_true", help="Call explicitly configured embeddings and transcript providers")
    parser.add_argument("--llm", action="store_true", help="Send one minimal JSON connectivity request to the configured chat model")
    args = parser.parse_args()
    if not settings.sec_user_agent or "@" not in settings.sec_user_agent:
        raise SystemExit("Set SEC_USER_AGENT in .env before running the live SEC check")
    sec = SecClient(settings.sec_user_agent, settings.sec_min_interval)
    filings = sec.filings(args.cik, ("10-K",), args.year)
    companyfacts = sec.companyfacts(args.cik)
    facts = sec.facts_for_year(args.cik, args.year, companyfacts)
    result = {"sec": {"filing_count": len(filings), "companyfacts_concepts": len(companyfacts.get("facts", {}).get("us-gaap", {})),
        "annual_metrics_found": sorted(facts), "year": args.year, "cik": args.cik}}
    if args.providers:
        embedding = EmbeddingClient()
        if embedding.configured:
            result["embeddings"] = {"model": embedding.identity, "dimensions": len(embedding.embed(["Meridian provider smoke check"])[0])}
        transcript = TranscriptClient()
        if transcript.configured:
            result["transcripts"] = {"endpoint_configured": True,
                "records_returned": len(transcript.for_year(args.cik, "SEC smoke check", args.year))}
    if args.llm:
        client = LLMClient()
        if not client.configured:
            raise SystemExit("Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL before using --llm")
        response, tokens = client.complete_json("Return the input object unchanged as JSON.", {"status": "ok"})
        result["chat_model"] = {"response": response, "tokens": tokens}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
