# Meridian

Meridian is a local-first comparative equity research application. It ingests SEC filings and user-supplied documents, keeps source metadata with retrieved passages, calculates selected metrics from SEC XBRL facts, and produces a memo whose material statements link to evidence IDs.

## Implementation status and remaining external inputs

The repository was empty when this work began: it had a Git directory but no commits or tracked source files. The current implementation establishes a connected application rather than a recovered baseline.

Implemented functionality:

- A searchable, editable Fortune 500 company registry with exact name, ticker, CIK, and alias resolution.
- SEC EDGAR submission discovery, 10-K/10-Q retrieval, and SEC Company Facts ingestion for reported XBRL values.
- Upload and extraction for text, filing HTML (including tables), and text-based PDFs. Empty-text PDF pages are selectively rendered and sent through configured Tesseract OCR or an explicitly enabled vision model.
- Persistent SQLite-backed RAG with company, year, and document-type filters. The default uses local Sentence Transformers semantic embeddings plus lexical matching; an OpenAI-compatible embeddings endpoint or deterministic feature-hash mode can be selected instead. Existing chunks can be reindexed when the model changes.
- Separate Risk, Strategy, Financial, and Synthesizer components. Risk and strategy use distinct domain rules and can call an optional OpenAI-compatible JSON chat endpoint. Disclosure claims require exact quoted evidence and IDs; an optional separate LLM support judge adds a probabilistic entailment check. Financial calculations retain formulas and cited inputs.
- Annual reported financial facts preserve their SEC currency. A supplied annual FX table can transparently normalize foreign currencies to USD and retains reported values and conversion provenance.
- A normalized transcript-service adapter, asynchronous research runs, stage traces, latency and provider token counts when present, a browser interface, Markdown/PDF memo exports, revenue charts, and archived upload downloads.
- A deterministic fixture evaluation command measuring context precision, recall, and exact-quote unsupported-claim rate.

Remaining requirements and boundaries:

- `data/fortune500.csv` remains a labeled 2026 seed containing 12 companies. The supplied 2023 file matches the public [EatMoreOranges dataset file](https://github.com/EatMoreOranges/Fortune-500-Dataset/blob/main/data/2023-fortune-500-data.csv); that repository says the data was scraped from 50Pros, but no explicit license grant was found. The normalized candidate is `data/fortune500_2023.csv`, with the source URL recorded, 500 rows, tied rank 230 preserved, and 431 CIKs resolved from SEC mappings; 69 remain unresolved. It is not active pending review of unresolved rows and confirmation of permitted use.
- Transcript retrieval is integrated through a provider adapter, but a licensed service and API endpoint must be configured. Its required JSON response contract is documented below. Without it, users can upload transcripts.
- OCR is available with the project-local Tesseract 5.4 runtime and Poppler available in this environment. Vision extraction still requires explicitly enabled vision-capable LLM credentials. Scanned-page OCR is exercised by local tests; no model-based interpretation of charts or figures has been accepted against live filings.
- Trained local semantic embeddings use `sentence-transformers/all-MiniLM-L6-v2` by default and cache weights under `models/cache`. The first use may need Hugging Face network access to verify model files. An OpenAI-compatible embedding endpoint or deterministic feature-hash mode can be chosen in configuration. Changing the provider or model requires `python scripts/reindex_embeddings.py` so stored vectors use one consistent model. The latest standalone model-load check did not complete during verification; the HTTP workflow was verified using the deterministic feature-hash fallback.
- Risk/strategy analyses without configured LLM credentials use explicit keyword rules. Those findings are intentionally conservative and labeled with heuristic uncertainty. Groq is the configured OpenAI-compatible chat default; adding a Groq API key enables model-assisted analysis.
- SEC facts cover common annual US-GAAP concepts. Currency units are preserved and optional annual FX rates normalize them. Inline XBRL issuer-specific tags and dimensioned facts are extracted into an analyst queue, and same-tag/same-dimension values are lined up across periods. Later-filing differences are possible restatements or reclassifications, never automatic conclusions. Accounting mappings, segment identity, and fiscal-period comparability still require analyst approval.
- Claim support uses exact quote, numeric-source, and evidence-ID checks. Exact quote matching does not establish entailment. When enabled, the optional LLM judge classifies support probabilistically; unjudged, partial, contradicted, or unclear claims are surfaced for human review. The local fixture benchmark remains a small, synthetic engineering check, not a live research-quality score.
- PDF export, financial revenue charts, and content-addressed retention of uploaded source files are implemented. SEC and Groq connectivity smoke checks passed with the configured credentials. Transcript and FX integrations remain unconfigured by request; vision and real-company research acceptance have not been exercised end to end.

The application does not generate market-price recommendations or substitute estimates for unavailable filing values.

## Requirements

- Python 3.10 or later.
- `pypdf` for PDF extraction, `reportlab` for PDF memo export, `sentence-transformers` for local semantic embeddings, and `httpx` for outbound OpenAI-compatible chat API calls. HTML, plain text, SQLite, and the HTTP server use the Python standard library.
- An SEC contact User-Agent to retrieve EDGAR data. SEC requests use a configurable delay and the identifying User-Agent header.
- Optional: a Groq API key for model-assisted Risk/Strategy findings and support judging; an embeddings endpoint; a transcript adapter service; and an annual FX CSV.
- Optional OCR: Poppler (`pdftoppm`) and Tesseract executables.

## Install and run

From the repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `SEC_USER_AGENT` in `.env` to a descriptive application name and an email inbox you own and check, for example `Meridian Research Aman aman@example.edu`. The name is just an identifier for this app; use a college or personal email you monitor. You do not need a separate SEC account. The SEC asks automated clients to identify themselves with a User-Agent and contact email ([SEC access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)).

Groq is the default chat provider. Set `LLM_API_KEY` in `.env` to the key from your Groq account; the configured base URL and model are `https://api.groq.com/openai/v1` and `openai/gpt-oss-20b`. Meridian calls the OpenAI-compatible endpoint directly, so a Groq SDK is not required. Model IDs can change; check the [current Groq model list](https://console.groq.com/docs/models) before switching `LLM_MODEL`. Leave `LLM_VISION_ENABLED=false` unless your chosen model supports image input. Local Sentence Transformers remains Meridian's embedding provider; Groq chat does not replace the RAG embeddings.

Start Meridian:

```powershell
python -m meridian.server
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server binds to localhost by default. Set `MERIDIAN_HOST` and `MERIDIAN_PORT` only when you intend to expose it on another interface.

## Fast presentation runs

For a classroom demo, select **Fast presentation run (use saved filings only)** before generating the memo. This mode skips SEC network requests and reuses indexed filings and cached XBRL facts. It cannot retrieve a filing or metric that is not already stored. If filings are already present but XBRL facts are missing, leave fast mode off for one run to cache those facts; Meridian now skips discovery and download for already-stored filings. First-time filing retrieval and embedding/indexing can take several minutes, depending on SEC response time and local CPU.

To prepare comparisons across the full configured universe, use **Prepare fast comparisons** before the presentation. Choose one to three fiscal years in the comparison form, then start the bulk cache job. Meridian retrieves and indexes each SEC-ready company's 10-K filing(s) and SEC Company Facts once, with live company counts and error reporting; the job runs sequentially and can take a long time for hundreds of companies. Leave the server running and online until it reports complete. Afterward, use the same year range, select 10-K, and turn on Fast mode. This shares cached company evidence between pairs; pair-specific agent analysis and memo generation still run for each comparison. It does not precompute tens of thousands of pair-specific memos, guarantee a 30-second end-to-end runtime, cache 10-Qs/transcripts, or add sources for companies without SEC CIKs/available filings. Cached data remains in Meridian's SQLite store when the app restarts.

Risk and strategy requests run concurrently. Semantic claim review is sent as one bounded batch instead of one request per claim. Chat requests have a 20-second timeout; when a specialist call times out, its local rule-based findings are used, and claims without a judge result remain explicitly queued for analyst review. The cached mode is designed for a short classroom wait, but an exact wall-clock guarantee is not possible across different computers and Groq/network conditions.

## Fortune 500 company universe

The default CSV records the 2026 ranking year and its source URL. For a full universe, use a Fortune 500 CSV you are entitled to use with at least these columns:

`company_name,ticker,cik,rank,reporting_year`

Optional columns are `industry`, `aliases` (pipe-separated), and `source_url`. Rows without a CIK can remain in the searchable ranking, but the UI disables SEC research for those rows until a CIK is resolved.

Import with the UI's **Refresh the company universe** section, or:

```powershell
python scripts/import_universe.py --csv C:\path\to\fortune500.csv --year 2026 --source-url https://fortune.com/ranking/fortune500/
```

The importer checks required columns, row count, CIKs, and duplicate companies before writing the requested output. It preserves tied ranks and does not silently fabricate CIKs or tickers.

The importer also accepts the supplied 2023 source layout (`Rank,Company,Ticker,Industry`) and can resolve missing public-company CIKs through the SEC ticker mapping after `SEC_USER_AGENT` is set:

```powershell
python scripts/import_universe.py --csv C:\path\to\2023-fortune-500-data.csv --year 2023 --resolve-ciks --output data\fortune500_2023.csv
```

Rows that remain without a CIK are searchable but not SEC research-ready. Set `MERIDIAN_UNIVERSE=data/fortune500_2023.csv` in `.env` to activate this historical ranking after reviewing unresolved rows and confirming you are entitled to use the source. Tied ranks are preserved and reported.

## API overview

- `GET /api/health` — configuration and registry status.
- `GET /api/companies?q=...` — company search.
- `GET /api/filings?cik=...&year=2025&forms=10-K,10-Q` — EDGAR discovery.
- `GET /api/documents?cik=...` — stored document metadata.
- `GET /api/documents/{document_id}/original` — download an archived user upload.
- `POST /api/upload` — multipart upload with company, document type, and fiscal period.
- `POST /api/research` — start an asynchronous comparison run.
- `POST /api/cache/warm` and `GET /api/cache/warm` — prepare/poll a background cache of annual SEC filings and XBRL facts for the configured company universe (one to three years).
- `GET /api/runs/{run_id}` and `/traces` — run state, output, and execution trace.
- `GET /api/runs/{run_id}/evidence/{evidence_id}` — inspect evidence in the completed run.
- `GET /api/runs/{run_id}/memo.md` — export Markdown.
- `GET /api/runs/{run_id}/memo.pdf` — export PDF.
- `GET /api/evaluation` — rerun the deterministic local fixture benchmark.
- `POST /api/universe/import` — import a multipart CSV universe.

Research request example:

```json
{
  "company_ciks": ["320193", "789019"],
  "years": [2023, 2024, 2025],
  "document_types": ["10-K", "10-Q", "transcript"],
  "fetch_sec": true
}
```

Fiscal years are selected by SEC report period; filing dates can fall in the following calendar year. If the pipeline cannot retrieve sufficient evidence for both companies, it fails with a specific error instead of producing an ungrounded memo.

## Optional provider contracts

### Earnings transcripts

Set `TRANSCRIPT_BASE_URL`, optional `TRANSCRIPT_API_KEY`, and `TRANSCRIPT_ENDPOINT` (default `/transcripts`). Meridian sends a GET request with `cik`, `year`, and `company` query parameters and a Bearer token when configured. The service must return JSON in this form:

```json
{"transcripts":[{"title":"Company Q1 2025 call","year":2025,"quarter":1,"content":"...full licensed transcript text...","source_url":"https://provider.example/transcript"}]}
```

`quarter` is optional; omit it for an annual `FY{year}` period. Only requested years are ingested. Confirm the provider license permits this use and local storage.

### Semantic embeddings

The default is a local Sentence Transformers model, `sentence-transformers/all-MiniLM-L6-v2`, stored under `models/cache`. Sentence Transformers converts passages and queries into dense vectors for semantic search; the SQLite store keeps those vectors beside the original evidence and filters before ranking. The model downloads on first use, and no embedding API key is required. The first local run needs internet access to fetch model files. The model is text-only; it does not read scanned pages or interpret charts.

Meridian's retrieval pipeline is intentionally composed from focused components rather than a LangChain or LlamaIndex orchestration layer: document extraction preserves source metadata, SQLite stores chunks and vectors, SQL filters company/period/document type before hybrid ranking, and the agents and claim validator carry evidence IDs through to the memo. Sentence Transformers supplies the pretrained semantic encoder. No missing framework dependency blocks the RAG workflow; adding a higher-level framework would be an optional architecture change.

To use another local Sentence Transformers model, set `EMBEDDING_MODEL` to its model ID or local path. To use an OpenAI-compatible provider instead, set `EMBEDDING_PROVIDER=api`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and optionally `EMBEDDING_API_KEY`. Meridian posts `{model,input:[...]}` to the base URL plus `/embeddings` and expects OpenAI-compatible `data` entries with `index` and `embedding`. `EMBEDDING_PROVIDER=hash` selects deterministic feature hashing without a learned semantic model. After changing the model or provider, run `python scripts/reindex_embeddings.py`; health status reports stale chunks.

### Annual FX rates

Copy `data/fx_annual_rates.example.csv` to the path in `FX_RATES_CSV`. Supply verified annual-average rates with columns `year,from_currency,to_currency,rate,source,rate_date`. `rate` means USD per one unit of `from_currency`, and `to_currency` must be USD. Meridian retains reported values, converted values, rate, and source; it never guesses a missing rate.

## Configuration

See `.env.example`. Main settings:

- `SEC_USER_AGENT` — required for SEC EDGAR.
- `SEC_MIN_REQUEST_INTERVAL` — seconds between SEC requests (default `0.12`).
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — Groq-compatible chat configuration by default; can be overridden with another OpenAI-compatible provider.
- `LLM_VISION_ENABLED`, `CLAIM_JUDGE_ENABLED` — explicit opt-ins for image transcription and semantic support judging.
- `PDFTOPPM_CMD`, `TESSERACT_CMD` — Poppler and Tesseract executable names or paths for selective OCR. This workspace has project-local Tesseract at `tools/tesseract/tesseract.exe`.
- `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_CACHE_DIR` — local Sentence Transformers by default, or `api` / `hash` modes.
- `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` — optional OpenAI-compatible vector embeddings.
- `TRANSCRIPT_BASE_URL`, `TRANSCRIPT_API_KEY`, `TRANSCRIPT_ENDPOINT` — optional licensed transcript adapter.
- `FX_RATES_CSV` — optional annual FX table path; conversion is only made when a matching sourced rate is present.
- `MERIDIAN_DB`, `MERIDIAN_UNIVERSE`, `MERIDIAN_UPLOAD_DIR` — local persistence paths.
- `RETRIEVAL_LIMIT` — maximum retrieved chunks per company and specialist query.
- `MAX_UPLOAD_MB` — HTTP request limit.

Do not commit `.env` or real API credentials.

## Tests and evaluation

Run automated tests:

```powershell
python -m unittest discover -s tests -v
```

Run the reproducible fixture evaluation:

```powershell
python scripts/evaluate.py
```

Run the local HTTP integration smoke test (localhost socket access required):

```powershell
python tests/local_e2e.py
```

Run read-only checks against configured SEC data providers:

```powershell
python scripts/smoke_live.py --cik 320193 --year 2025
python scripts/smoke_live.py --cik 320193 --year 2025 --providers
python scripts/smoke_live.py --cik 320193 --year 2025 --llm
```

The evaluation prints dataset composition, methodology, context precision, context recall, unsupported-claim rate, and each case. It uses small deterministic evidence passages, not real company documents. It must not be presented as evidence of live SEC retrieval performance.

Latest verification: all 20 unit tests pass, including scanned-PDF OCR, analyst-review cases, automatic cache reuse, fast cached mode, and uneven SEC submissions fields; Python compilation and JavaScript syntax pass. The local HTTP workflow passed with synthetic uploads, 4 evidence blocks, 4 claims retained after evidence checks, all expected research traces, Markdown/PDF export, and original-upload download. Live SEC retrieval passed for Apple (CIK 320193), returning a 2025 10-K and 12 annual metrics; Groq returned valid JSON through HTTPX. SEC ticker mapping resolved 431 of the 500 2023 candidate companies. The five-case fixture evaluation measured context precision 0.60, recall 1.00, and unsupported-claim rate 0.167 (1 of 6 claims); it is a small synthetic engineering check, not live-company research-quality validation. The standalone local Sentence Transformers load did not finish during this check, so local semantic inference still needs confirmation. No transcript or FX provider run was performed.

## Data handling and trace contents

Uploaded original bytes are retained under `MERIDIAN_UPLOAD_DIR/originals` in content-addressed files; document metadata and extracted evidence are stored in SQLite. The original is downloadable using its document ID. SEC downloads are retained as extracted text/table evidence with source URLs, accession numbers, content hashes, and access timestamps. The application records stage inputs/outputs, evidence counts, status, latency, and provider token counts when the provider returns them. It does not store private model chain-of-thought. Transcript content is stored locally when returned by a configured service, so confirm its license permits retention.

Company and fiscal-period filtering is applied before ranking retrieved passages. The evidence register reports page or table locations only when extraction actually found them. SEC XBRL evidence names the taxonomy tag and filing accession; exact PDF page placement is unavailable for these API facts.

## Acceptance status at handoff

Implemented: local web UI/API, company registry search/import, SEC submission and companyfacts adapters, filing and manual ingestion, persistent filtered hybrid retrieval with local Sentence Transformers embeddings, specialist analysis, metric calculations, source-linked memo, citation checks, traces, Markdown/PDF export, revenue charts, upload retention, fixture evaluation, project-local OCR plus optional vision fallback, transcript adapter, configurable embedding providers, sourced annual FX normalization, Inline XBRL custom-tag and dimensioned-fact review candidates, possible restatement reconciliation, and semantic claim review queues.

Remaining setup depends on the feature used: configure a transcript service URL/key (or upload transcripts manually) to use automatic transcript retrieval, and supply sourced annual FX rates to compare non-USD values. SEC and Groq connectivity are configured and smoke-tested. The local Sentence Transformers model was freshly verified in the user's environment and returned a normalized 384-dimensional vector; the project-local Tesseract OCR runtime is cached in this workspace. The active research universe is the 431 CIK-mapped companies from the 2023 Fortune 500 candidate, resolved against the SEC's current ticker mapping. The original 500-row source is retained; 69 unresolved rows are excluded from SEC research pending mapping. Because the mapping is current rather than historical, some 2023 issuer identities may need checking. The claim judge remains opt-in (`CLAIM_JUDGE_ENABLED=false` by default), so current exact-quote checks validate citation presence, not semantic entailment. Custom-tag mappings, segment identity/comparability, interpretation of changed facts, semantic entailment, and investment judgments are review points for correctness and coverage, not performance blockers. No live-company investment recommendation is generated.
