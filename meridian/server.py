from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import traceback
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import ROOT, settings
from .cache_warm import warm_company_cache
from .evaluation import run_fixture_evaluation
from .pipeline import ingest_upload, markdown_memo, run_research
from .memo_pdf import build_memo_pdf
from .registry import CompanyRegistry
from .sec import SecClient
from .store import Store
from .transcripts import TranscriptClient


store = Store(settings.db_path)
registry = CompanyRegistry(settings.universe_path)
sec = SecClient(settings.sec_user_agent, settings.sec_min_interval)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
RUN_THREADS: dict[str, threading.Thread] = {}
RUN_SEMAPHORE = threading.Semaphore(2)
CACHE_WARM_LOCK = threading.Lock()
CACHE_WARM_STATE: dict = {"state": "idle"}
CACHE_WARM_CANCEL_EVENT = threading.Event()


def _cache_warm_job(years: list[int], registry_snapshot: CompanyRegistry):
    global CACHE_WARM_STATE
    try:
        eligible = [company for company in registry_snapshot.companies if company.cik]
        def update(state):
            global CACHE_WARM_STATE
            with CACHE_WARM_LOCK:
                CACHE_WARM_STATE = dict(state, years=years,
                    universe_total=len(eligible), started_at=CACHE_WARM_STATE.get("started_at"))
        update({"state": "running", "companies_total": len(eligible), "companies_done": 0,
                "filings_added": 0, "facts_added": 0, "errors": []})
        warm_company_cache(eligible, years, store, sec, update, cancelled=CACHE_WARM_CANCEL_EVENT)
        with CACHE_WARM_LOCK:
            if CACHE_WARM_CANCEL_EVENT.is_set():
                CACHE_WARM_STATE["state"] = "cancelled"
            elif CACHE_WARM_STATE.get("state") == "running":
                CACHE_WARM_STATE["state"] = "complete"
    except Exception as exc:
        with CACHE_WARM_LOCK:
            CACHE_WARM_STATE = {**CACHE_WARM_STATE, "state": "failed", "fatal_error": str(exc)[:500]}


def _run_job(run_id: str, request: dict, registry_snapshot: CompanyRegistry):
    try:
        with RUN_SEMAPHORE:
            run_research(run_id, request, registry_snapshot, store, sec)
    finally:
        RUN_THREADS.pop(run_id, None)


class Handler(SimpleHTTPRequestHandler):
    server_version = "Meridian/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format, *args):
        print("[meridian] " + format % args)

    def _json(self, status: int, body):
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _text(self, status: int, body: str, content_type="text/plain; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _binary(self, status: int, payload: bytes, content_type: str, filename: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > settings.max_upload_mb * 1024 * 1024:
            raise ValueError(f"Request body is empty or exceeds {settings.max_upload_mb} MB")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        body = self._body()
        return json.loads(body.decode("utf-8"))

    def _multipart(self):
        raw = self._body()
        message = f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
        # email.parser handles standards-compliant multipart bodies without cgi.parse_multipart's nested lists.
        from email import policy
        from email.parser import BytesParser
        parsed = BytesParser(policy=policy.default).parsebytes(message)
        fields, files = {}, {}
        for part in parsed.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            value = part.get_payload(decode=True) or b""
            if filename is not None:
                files[name] = {"filename": Path(filename).name, "content": value,
                               "content_type": part.get_content_type()}
            else:
                fields[name] = value.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                from .embeddings import EmbeddingClient
                embedding_client = EmbeddingClient()
                tesseract_available = bool(shutil.which(settings.tesseract_cmd) or Path(settings.tesseract_cmd).is_file())
                poppler_available = bool(shutil.which(settings.pdftoppm_cmd) or Path(settings.pdftoppm_cmd).is_file())
                self._json(200, {"ok": True, "version": "0.1.0", "universe": registry.source,
                    "sec_configured": bool(settings.sec_user_agent and "@" in settings.sec_user_agent),
                    "llm_configured": bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model),
                    "pdf_support": self._has_pypdf(), "pdf_export": self._has_reportlab(),
                    "transcript_provider": TranscriptClient().configured,
                    "vision_processing": bool(settings.vision_enabled and settings.llm_base_url and settings.llm_api_key and settings.llm_model),
                    "ocr_processing": bool(tesseract_available and poppler_available),
                    "embedding_provider": embedding_client.identity,
                    "embedding_configured": embedding_client.configured,
                    "embedding_index": store.embedding_index_status(embedding_client.identity)})
                return
            if path == "/api/cache/warm":
                with CACHE_WARM_LOCK:
                    self._json(200, dict(CACHE_WARM_STATE))
                return
            if path == "/api/companies":
                q = query.get("q", [""])[0]
                limit = min(500, max(1, int(query.get("limit", ["50"])[0])))
                self._json(200, {"source": registry.source,
                    "companies": [c.as_dict() for c in registry.search(q, limit)]})
                return
            match = re.fullmatch(r"/api/company/(\d+)", path)
            if match:
                company = registry.by_cik(match.group(1))
                if not company:
                    self._json(404, {"error": "Company not found in the configured Fortune 500 universe"})
                else:
                    docs = store.list_documents([company.cik])
                    self._json(200, {"company": company.as_dict(), "documents": docs})
                return
            if path == "/api/filings":
                cik = query.get("cik", [""])[0]
                company = registry.by_cik(cik)
                if not company:
                    self._json(404, {"error": "Company not found in the configured universe"})
                    return
                forms = tuple(x for x in query.get("forms", ["10-K,10-Q"])[0].split(",") if x in {"10-K", "10-Q"})
                year = int(query["year"][0]) if query.get("year") else None
                filings = sec.filings(cik, forms or ("10-K", "10-Q"), year)
                self._json(200, {"company": company.company_name, "filings": filings})
                return
            if path == "/api/documents":
                ciks = [x for x in query.get("cik", []) if x]
                self._json(200, {"documents": store.list_documents(ciks or None)})
                return
            match = re.fullmatch(r"/api/documents/([a-f0-9-]+)/original", path)
            if match:
                doc = store.get_document(match.group(1))
                if not doc or not doc.get("metadata", {}).get("original_archived"):
                    self._json(404, {"error": "Original upload is not available"})
                    return
                extension = Path(doc.get("original_filename") or "").suffix.lower()
                if extension not in {".pdf", ".html", ".htm", ".xhtml", ".txt"} or not re.fullmatch(r"[a-f0-9]{64}", doc.get("sha256", "")):
                    self._json(404, {"error": "Original upload is not available"})
                    return
                archive = settings.upload_dir / "originals" / f"{doc['sha256']}{extension}"
                if not archive.is_file():
                    self._json(404, {"error": "Archived original file is missing"})
                    return
                content_type = doc.get("metadata", {}).get("content_type") or mimetypes.guess_type(doc.get("original_filename", ""))[0] or "application/octet-stream"
                self._binary(200, archive.read_bytes(), content_type,
                    Path(doc.get("original_filename") or f"source{extension}").name)
                return
            if path == "/api/evaluation":
                result = run_fixture_evaluation(ROOT / "data" / "evaluation_cases.json")
                store.save_evaluation(None, result["dataset_name"], result)
                self._json(200, result)
                return
            match = re.fullmatch(r"/api/runs/([a-f0-9-]+)(?:/(traces|memo\.md|memo\.pdf|evidence/([a-f0-9-]+)))?", path)
            if match:
                run_id, subpath, evidence_id = match.groups()
                run = store.get_run(run_id)
                if not run:
                    self._json(404, {"error": "Research run not found"})
                    return
                if subpath == "traces":
                    self._json(200, {"traces": store.traces(run_id)})
                elif subpath == "memo.md":
                    if not run.get("result"):
                        self._text(409, "Memo is not ready")
                    else:
                        self._text(200, markdown_memo(run["result"]), "text/markdown; charset=utf-8")
                elif subpath == "memo.pdf":
                    if not run.get("result"):
                        self._text(409, "Memo is not ready")
                    elif not self._has_reportlab():
                        self._json(503, {"error": "PDF export requires reportlab. Install project requirements with pip install -r requirements.txt"})
                    else:
                        filename = re.sub(r"[^A-Za-z0-9_-]+", "_", "_vs_".join(run["result"]["memo"].get("companies", []))).strip("_") or "research_memo"
                        self._binary(200, build_memo_pdf(run["result"]), "application/pdf", f"Meridian_{filename}.pdf")
                elif evidence_id:
                    evidence = (run.get("result") or {}).get("memo", {}).get("evidence", [])
                    item = next((x for x in evidence if x.get("evidence_id") == evidence_id), None)
                    self._json(200 if item else 404, item or {"error": "Evidence not found in this run"})
                else:
                    self._json(200, {"run_id": run_id, "status": run["status"],
                        "created_at": run["created_at"], "updated_at": run["updated_at"],
                        "error": run["error"], "result": run["result"]})
                return
            if path.startswith("/api/"):
                self._json(404, {"error": "API route not found"})
                return
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        global registry, CACHE_WARM_STATE
        path = urlparse(self.path).path
        try:
            if path == "/api/research":
                request = self._json_body()
                run_id = store.create_run(request)
                worker = threading.Thread(target=_run_job,
                    args=(run_id, request, registry), daemon=True,
                    name=f"meridian-{run_id[:8]}")
                RUN_THREADS[run_id] = worker
                worker.start()
                self._json(202, {"run_id": run_id, "status": "queued"})
                return
            if path == "/api/cache/warm":
                request = self._json_body()
                years = sorted({int(year) for year in request.get("years", [])})
                if not years or len(years) > 3 or min(years) < 1990 or max(years) > 2100:
                    raise ValueError("Choose between one and three valid fiscal years for cache preparation")
                if not settings.sec_user_agent or "@" not in settings.sec_user_agent:
                    raise ValueError("Configure SEC_USER_AGENT before preparing the company cache")
                with CACHE_WARM_LOCK:
                    if CACHE_WARM_STATE.get("state") in {"queued", "running"}:
                        self._json(409, {"error": "Cache preparation is already running"})
                        return
                    CACHE_WARM_CANCEL_EVENT.clear()
                    eligible_count = sum(bool(company.cik) for company in registry.companies)
                    CACHE_WARM_STATE = {"state": "queued", "companies_total": eligible_count,
                        "companies_done": 0, "filings_added": 0, "facts_added": 0, "errors": [],
                        "years": years, "started_at": datetime.now().astimezone().isoformat()}
                worker = threading.Thread(target=_cache_warm_job, args=(years, registry),
                    daemon=True, name="meridian-cache-warm")
                worker.start()
                self._json(202, {"status": "queued", "companies_total": eligible_count, "years": years})
                return
            if path == "/api/cache/warm/cancel":
                with CACHE_WARM_LOCK:
                    if CACHE_WARM_STATE.get("state") in {"queued", "running"}:
                        CACHE_WARM_CANCEL_EVENT.set()
                        CACHE_WARM_STATE["state"] = "cancelling"
                        self._json(200, {"status": "cancelling"})
                        return
                    self._json(200, {"status": CACHE_WARM_STATE.get("state", "idle")})
                return
            if path == "/api/upload":
                fields, files = self._multipart()
                file = files.get("file")
                if not file:
                    raise ValueError("Choose a document to upload")
                cik = fields.get("company_cik", "")
                company = registry.by_cik(cik)
                if not company:
                    raise ValueError("Select a company from the configured universe")
                fiscal_period = fields.get("fiscal_period", "").strip()
                if not re.fullmatch(r"FY\d{4}|Q[1-4] \d{4}", fiscal_period):
                    raise ValueError("Period must be FY2025 or Q1 2025")
                result = ingest_upload(store, company, file["filename"], file["content"],
                    fields.get("document_type", "other"), fiscal_period,
                    fields.get("title", ""), fields.get("source_url") or None, file.get("content_type", ""))
                self._json(201, result)
                return
            if path == "/api/universe/import":
                fields, files = self._multipart()
                upload = files.get("file")
                if not upload:
                    raise ValueError("Choose a Fortune 500 CSV file")
                reader = csv.DictReader(io.StringIO(upload["content"].decode("utf-8-sig")))
                required = {"company_name", "ticker", "cik", "rank", "reporting_year"}
                if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                    raise ValueError("CSV headers must include company_name,ticker,cik,rank,reporting_year")
                rows = list(reader)
                if not 2 <= len(rows) <= 500:
                    raise ValueError("Provide between 2 and 500 companies")
                seen_ciks, seen_ranks = set(), set()
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=["company_name", "ticker", "cik", "rank", "reporting_year", "industry", "aliases", "source_url"])
                writer.writeheader()
                for row in rows:
                    cik_digits = re.sub(r"\D", "", row.get("cik", ""))
                    if not row.get("company_name", "").strip() or not re.fullmatch(r"\d{1,10}", cik_digits):
                        raise ValueError("Every company needs a name and SEC CIK")
                    cik = str(int(cik_digits))
                    if cik in seen_ciks:
                        raise ValueError(f"Duplicate CIK: {cik}")
                    seen_ciks.add(cik)
                    try:
                        rank, year = int(row["rank"]), int(row["reporting_year"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError(f"Invalid rank or reporting year for {row['company_name']}") from exc
                    if not 1 <= rank <= 500 or not 1990 <= year <= 2100:
                        raise ValueError(f"Rank or reporting year is out of range for {row['company_name']}")
                    if rank in seen_ranks:
                        raise ValueError(f"Duplicate rank: {rank}")
                    seen_ranks.add(rank)
                    row["cik"] = cik
                    row["source_url"] = fields.get("source_url", "") or row.get("source_url", "")
                    writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
                settings.universe_path.parent.mkdir(parents=True, exist_ok=True)
                temp = settings.universe_path.with_suffix(".csv.tmp")
                temp.write_text(output.getvalue(), encoding="utf-8")
                os.replace(temp, settings.universe_path)
                registry = CompanyRegistry(settings.universe_path)
                self._json(200, {"source": registry.source, "companies_loaded": len(rows)})
                return
            self._json(404, {"error": "API route not found"})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            print(traceback.format_exc())
            self._json(500, {"error": str(exc)})

    @staticmethod
    def _has_pypdf() -> bool:
        try:
            import pypdf  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _has_reportlab() -> bool:
        try:
            import reportlab  # noqa: F401
            return True
        except ImportError:
            return False


def main():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    def _warmup_background():
        try:
            from .embeddings import EmbeddingClient
            EmbeddingClient().warmup()
        except Exception:
            pass
    threading.Thread(target=_warmup_background, daemon=True, name="meridian-embedding-warmup").start()
    host = os.getenv("MERIDIAN_HOST", "127.0.0.1")
    port = int(os.getenv("MERIDIAN_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Meridian is running at http://{host}:{port}")
    print(f"Loaded {registry.source['count']} company record(s); ranking years: {registry.source['reporting_years']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Meridian")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
