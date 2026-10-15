"""Local HTTP-to-memo smoke test using clearly synthetic test-only text fixtures."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / ".tmp"
TMP.mkdir(exist_ok=True)
DB = TMP / f"api-e2e-{uuid.uuid4().hex}.sqlite3"
os.environ["MERIDIAN_DB"] = str(DB)
os.environ["MERIDIAN_UNIVERSE"] = str(ROOT / "data" / "fortune500.csv")
os.environ["MERIDIAN_UPLOAD_DIR"] = str(TMP / "uploads")
# Keep this synthetic HTTP fixture deterministic and independent of downloaded model weights.
os.environ["EMBEDDING_PROVIDER"] = "hash"
# Never send synthetic fixture content to a configured external chat provider.
os.environ["LLM_API_KEY"] = ""
os.environ["CLAIM_JUDGE_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from meridian.server import Handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def http_json(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return response.status, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw.decode()


def multipart(fields, filename, content):
    boundary = "----MeridianBoundary" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n".encode(), str(value).encode(), b"\r\n"])
    parts.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n".encode(), content, b"\r\n", f"--{boundary}--\r\n".encode()])
    return boundary, b"".join(parts)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    fixture_documents = [
        ("320193", "Apple", b"ITEM 1A. RISK FACTORS\nWe face cybersecurity threats that could disrupt customer services. We also rely on suppliers in a concentrated global supply chain.\nITEM 7. MANAGEMENT DISCUSSION\nWe plan capital expenditures to expand manufacturing capacity and invest in research and development."),
        ("789019", "Microsoft", b"ITEM 1A. RISK FACTORS\nCompetition and regulatory scrutiny could affect our cloud business and customer relationships.\nITEM 7. MANAGEMENT DISCUSSION\nManagement expects to expand cloud capacity and continue investment in artificial intelligence infrastructure.")
    ]
    try:
        health_status, health = http_json(base + "/api/health")
        assert health_status == 200 and health["universe"]["count"] >= 2
        page_status, page = http_json(base + "/")
        assert page_status == 200 and "financial-table" in page
        asset_status, app_script = http_json(base + "/app.js")
        assert asset_status == 200 and "Generate research memo" in app_script
        for cik, name, content in fixture_documents:
            boundary, body = multipart({"company_cik": cik, "document_type": "10-K",
                "fiscal_period": "FY2024", "title": f"{name} test fixture 10-K"}, f"{name.lower()}_fixture.txt", content)
            status, uploaded = http_json(base + "/api/upload", "POST", body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"})
            assert status == 201 and uploaded["block_count"] >= 1
            if cik == "320193":
                original_id = uploaded["document_id"]
                original_url = base + f"/api/documents/{original_id}/original"
                with urllib.request.urlopen(original_url, timeout=10) as response:
                    assert response.read() == content
        status, started = http_json(base + "/api/research", "POST",
            json.dumps({"company_ciks": ["320193", "789019"], "years": [2024],
                "document_types": ["10-K"], "fetch_sec": False}).encode(),
            {"Content-Type": "application/json"})
        assert status == 202
        run_id = started["run_id"]
        for _ in range(150):
            _, run = http_json(base + f"/api/runs/{run_id}")
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.1)
        assert run["status"] in {"complete", "complete_with_gaps"}, run
        result = run["result"]
        assert len(result["memo"]["evidence"]) >= 2
        assert result["memo"]["validation"]["claims_supported"] >= 1
        _, trace_data = http_json(base + f"/api/runs/{run_id}/traces")
        stages = {trace["stage"] for trace in trace_data["traces"]}
        assert {"risk_analysis", "strategy_analysis", "financial_analysis", "synthesis", "evidence_validation"}.issubset(stages)
        _, markdown = http_json(base + f"/api/runs/{run_id}/memo.md")
        assert "Risk comparison" in markdown and "Evidence validation" in markdown
        try:
            with urllib.request.urlopen(base + f"/api/runs/{run_id}/memo.pdf", timeout=20) as response:
                pdf = response.read()
                assert response.headers.get("Content-Type") == "application/pdf" and pdf.startswith(b"%PDF-")
        except urllib.error.HTTPError as exc:
            raise AssertionError(f"PDF export failed: {exc.read().decode('utf-8', errors='replace')}") from exc
        pdf_output = os.environ.get("MERIDIAN_TEST_PDF_OUTPUT")
        if pdf_output:
            target = Path(pdf_output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pdf)
        print(json.dumps({"status": run["status"], "evidence_count": len(result["memo"]["evidence"]),
            "supported_claims": result["memo"]["validation"]["claims_supported"],
            "trace_stages": sorted(stages), "markdown_export": "passed", "pdf_export": "passed",
            "original_upload_download": "passed",
            "data_note": "Synthetic fixture documents used only for local HTTP integration; no real-provider test."}, indent=2))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(DB) + suffix)
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    main()
