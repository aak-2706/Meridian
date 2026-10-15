from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import settings


class TranscriptClient:
    """Adapter for transcript services exposing Meridian's normalized JSON contract.

    GET {TRANSCRIPT_BASE_URL}{TRANSCRIPT_ENDPOINT}?cik=...&year=...&company=...
    Response: {"transcripts": [{"title", "year", "quarter", "content", "source_url"}]}
    """

    def __init__(self):
        self.base_url = settings.transcript_base_url
        self.endpoint = settings.transcript_endpoint or "/transcripts"
        self.api_key = settings.transcript_api_key

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def for_year(self, cik: str, company: str, year: int) -> list[dict]:
        if not self.configured:
            return []
        endpoint = urllib.parse.urljoin(self.base_url + "/", self.endpoint.lstrip("/"))
        query = urllib.parse.urlencode({"cik": cik, "year": year, "company": company})
        headers = {"Accept": "application/json", "User-Agent": "Meridian equity research workspace"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{endpoint}?{query}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Transcript provider request failed: {exc}") from exc
        rows = payload if isinstance(payload, list) else payload.get("transcripts", [])
        if not isinstance(rows, list):
            raise RuntimeError("Transcript provider response must contain a transcripts array")
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("content") or row.get("text")
            row_year = row.get("year") or year
            quarter = row.get("quarter")
            try:
                row_year = int(str(row_year)[:4])
            except (TypeError, ValueError):
                continue
            if row_year != year or not isinstance(text, str) or not text.strip():
                continue
            if quarter:
                quarter = str(quarter).upper().removeprefix("Q")
                if quarter not in {"1", "2", "3", "4"}:
                    continue
                period = f"Q{quarter} {year}"
            else:
                period = f"FY{year}"
            source_url = row.get("source_url") or row.get("url")
            if source_url and urllib.parse.urlparse(source_url).scheme not in {"http", "https"}:
                source_url = None
            normalized.append({"title": str(row.get("title") or f"{company} earnings-call transcript {period}"),
                "period": period, "text": text, "source_url": source_url})
        return normalized
