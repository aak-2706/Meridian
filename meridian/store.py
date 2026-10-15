from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .embeddings import EmbeddingClient


_TOKEN = re.compile(r"[a-z0-9]+(?:['-][a-z0-9]+)?", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vector(text: str, dimensions: int = 384) -> list[float]:
    counts: dict[int, float] = {}
    for token in _TOKEN.findall(text.lower()):
        if len(token) < 2:
            continue
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "little")
        idx = raw % dimensions
        sign = -1.0 if raw & (1 << 63) else 1.0
        counts[idx] = counts.get(idx, 0.0) + sign
    norm = math.sqrt(sum(x * x for x in counts.values())) or 1.0
    return [counts.get(i, 0.0) / norm for i in range(dimensions)]


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._vec_cache: dict[str, list[float]] = {}
        self._init()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS documents(
              doc_id TEXT PRIMARY KEY, company_cik TEXT NOT NULL, company_name TEXT NOT NULL,
              document_type TEXT NOT NULL, fiscal_period TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
              source_url TEXT, accession TEXT, sha256 TEXT NOT NULL, access_date TEXT NOT NULL,
              original_filename TEXT, extraction_method TEXT NOT NULL, block_count INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_hash_company ON documents(company_cik, sha256);
            CREATE INDEX IF NOT EXISTS idx_doc_accession ON documents(company_cik, accession);
            CREATE INDEX IF NOT EXISTS idx_doc_company_period ON documents(company_cik, fiscal_period, document_type);
            CREATE TABLE IF NOT EXISTS chunks(
              chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
              company_cik TEXT NOT NULL, company_name TEXT NOT NULL, document_type TEXT NOT NULL,
              fiscal_period TEXT NOT NULL, page_number INTEGER, section TEXT, table_id TEXT,
              content TEXT NOT NULL, content_type TEXT NOT NULL, extraction_method TEXT NOT NULL,
              vector_json TEXT NOT NULL, embedding_model TEXT NOT NULL DEFAULT 'local-feature-hash-384');
            CREATE INDEX IF NOT EXISTS idx_chunks_filter ON chunks(company_cik, fiscal_period, document_type);
            CREATE TABLE IF NOT EXISTS runs(
              run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS traces(
              trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
              stage TEXT NOT NULL, agent TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL,
              completed_at TEXT, latency_ms INTEGER, input_json TEXT, output_json TEXT,
              error TEXT, token_usage INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id, started_at);
            CREATE TABLE IF NOT EXISTS evaluations(
              evaluation_id TEXT PRIMARY KEY, run_id TEXT, created_at TEXT NOT NULL,
              dataset_name TEXT NOT NULL, result_json TEXT NOT NULL);
            """)
            chunk_columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
            if "embedding_model" not in chunk_columns:
                db.execute("ALTER TABLE chunks ADD COLUMN embedding_model TEXT NOT NULL DEFAULT 'local-feature-hash-384'")

    def add_document(self, company: dict, document_type: str, fiscal_period: str, title: str,
                     source_url: str | None, sha256: str, blocks: list, accession: str = "",
                     filename: str = "", metadata: dict | None = None) -> tuple[str, bool]:
        with self._lock, self.connect() as db:
            prior = db.execute("SELECT doc_id FROM documents WHERE company_cik=? AND sha256=?",
                               (company["cik"], sha256)).fetchone()
            if prior:
                return prior["doc_id"], False
            doc_id = str(uuid.uuid4())
            embedding_client = EmbeddingClient()
            vectors = embedding_client.embed([block.content for block in blocks])
            methods = sorted({b.extraction_method for b in blocks})
            db.execute("INSERT INTO documents(doc_id,company_cik,company_name,document_type,fiscal_period,title,source_url,accession,sha256,access_date,original_filename,extraction_method,block_count,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (doc_id, company["cik"], company["company_name"], document_type, fiscal_period,
               title, source_url, accession, sha256, utcnow(), filename, ",".join(methods), len(blocks),
               json.dumps(metadata or {})))
            chunk_rows = []
            for block, vector in zip(blocks, vectors):
                chunk_id = str(uuid.uuid4())
                chunk_rows.append((chunk_id, doc_id, company["cik"], company["company_name"], document_type,
                   fiscal_period, block.page_number, block.section, block.table_id,
                   block.content, block.content_type, block.extraction_method,
                   json.dumps(vector), embedding_client.identity))
                if len(self._vec_cache) < 20000:
                    self._vec_cache[chunk_id] = vector
            if chunk_rows:
                db.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", chunk_rows)
            return doc_id, True

    def has_accession(self, company_cik: str, accession: str) -> bool:
        if not accession:
            return False
        with self.connect() as db:
            return db.execute("SELECT 1 FROM documents WHERE company_cik=? AND accession=? LIMIT 1",
                              (company_cik, accession)).fetchone() is not None

    def document_by_accession(self, company_cik: str, accession: str) -> dict | None:
        if not accession:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE company_cik=? AND accession=? LIMIT 1",
                             (company_cik, accession)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        except (KeyError, json.JSONDecodeError):
            result["metadata"] = {}
        return result

    def list_documents(self, company_ciks: list[str] | None = None, period: str = "") -> list[dict]:
        query = "SELECT * FROM documents WHERE 1=1"
        args = []
        if company_ciks:
            query += " AND company_cik IN (" + ",".join("?" for _ in company_ciks) + ")"
            args.extend(company_ciks)
        if period:
            query += " AND fiscal_period LIKE ?"
            args.append(f"%{period}%")
        query += " ORDER BY access_date DESC"
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args)]

    def get_document(self, doc_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            try:
                result["metadata"] = json.loads(result.pop("metadata_json"))
            except (KeyError, json.JSONDecodeError):
                result["metadata"] = {}
            return result

    def update_document_metadata(self, doc_id: str, updates: dict) -> None:
        with self.connect() as db:
            row = db.execute("SELECT metadata_json FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if not row:
                return
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update(updates)
            db.execute("UPDATE documents SET metadata_json=? WHERE doc_id=?", (json.dumps(metadata), doc_id))

    def cached_xbrl_facts(self, company_cik: str, periods: list[str]) -> dict[str, dict]:
        if not periods:
            return {}
        with self.connect() as db:
            docs = db.execute("SELECT * FROM documents WHERE company_cik=? AND document_type='XBRL' AND fiscal_period IN (" + ",".join("?" for _ in periods) + ")",
                [company_cik, *periods]).fetchall()
            output = {}
            for doc in docs:
                metadata = json.loads(doc["metadata_json"] or "{}")
                facts = metadata.get("facts")
                if not isinstance(facts, dict):
                    continue
                chunks = db.execute("SELECT chunk_id,table_id FROM chunks WHERE doc_id=?", (doc["doc_id"],)).fetchall()
                ids_by_tag = {row["table_id"]: row["chunk_id"] for row in chunks if row["table_id"]}
                for metric, fact in facts.items():
                    if isinstance(fact, dict):
                        fact["evidence_ids"] = [ids_by_tag[fact["tag"]]] if fact.get("tag") in ids_by_tag else []
                output[doc["fiscal_period"]] = facts
            return output

    def search(self, query: str, company_ciks: list[str], periods: list[str] | None = None,
               document_types: list[str] | None = None, limit: int = 8) -> list[dict]:
        if not company_ciks:
            return []
        clauses = ["c.company_cik IN (" + ",".join("?" for _ in company_ciks) + ")"]
        args: list = list(company_ciks)
        if periods:
            period_parts = []
            for period in periods:
                year = re.search(r"\d{4}", period)
                period_parts.append("c.fiscal_period=?")
                args.append(period)
                if year:
                    period_parts.append("c.fiscal_period LIKE ?")
                    args.append(f"%{year.group(0)}%")
            clauses.append("(" + " OR ".join(period_parts) + ")")
        if document_types:
            clauses.append("c.document_type IN (" + ",".join("?" for _ in document_types) + ")")
            args.extend(document_types)
        with self.connect() as db:
            rows = db.execute("SELECT c.*,d.title,d.source_url,d.accession,d.sha256 FROM chunks c JOIN documents d USING(doc_id) WHERE " + " AND ".join(clauses), args).fetchall()
        embedding_client = EmbeddingClient()
        candidates = [row for row in rows if row["embedding_model"] == embedding_client.identity]
        if not candidates:
            return []
        query_vec = embedding_client.embed_query(query)
        if not query_vec:
            return []
        query_terms = set(_TOKEN.findall(query.lower()))
        scored = []
        vec_cache = getattr(self, "_vec_cache", None)
        if vec_cache is None:
            self._vec_cache = {}
            vec_cache = self._vec_cache
        try:
            import numpy as np
            q_arr = np.array(query_vec, dtype=np.float32)
            c_vecs = []
            for row in candidates:
                cid = row["chunk_id"]
                vec = vec_cache.get(cid)
                if vec is None:
                    vec = json.loads(row["vector_json"])
                    if len(vec_cache) < 20000:
                        vec_cache[cid] = vec
                c_vecs.append(vec)
            mat = np.array(c_vecs, dtype=np.float32)
            semantics = np.dot(mat, q_arr)
            for idx, row in enumerate(candidates):
                semantic = float(semantics[idx])
                doc_terms = set(_TOKEN.findall(row["content"].lower()))
                lexical = len(query_terms & doc_terms) / (len(query_terms) or 1)
                score = max(0.0, semantic) * 0.75 + lexical * 0.25
                if score <= 0:
                    continue
                item = dict(row)
                item["score"] = round(score, 6)
                item.pop("vector_json", None)
                item.pop("embedding_model", None)
                scored.append(item)
        except Exception:
            for row in candidates:
                cid = row["chunk_id"]
                vec = vec_cache.get(cid)
                if vec is None:
                    vec = json.loads(row["vector_json"])
                    if len(vec_cache) < 20000:
                        vec_cache[cid] = vec
                semantic = sum(a * b for a, b in zip(query_vec, vec))
                doc_terms = set(_TOKEN.findall(row["content"].lower()))
                lexical = len(query_terms & doc_terms) / (len(query_terms) or 1)
                score = max(0.0, semantic) * 0.75 + lexical * 0.25
                if score <= 0:
                    continue
                item = dict(row)
                item["score"] = round(score, 6)
                item.pop("vector_json", None)
                item.pop("embedding_model", None)
                scored.append(item)
        return sorted(scored, key=lambda x: x["score"], reverse=True)[:max(1, min(limit, 50))]

    def embedding_index_status(self, current_model: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT embedding_model,COUNT(*) AS count FROM chunks GROUP BY embedding_model").fetchall()
        counts = {row["embedding_model"]: int(row["count"]) for row in rows}
        return {"current_model": current_model, "indexed_chunks": counts.get(current_model, 0),
            "stale_chunks": sum(count for model, count in counts.items() if model != current_model),
            "models": counts}

    def reindex_embeddings(self, batch_size: int = 32) -> dict:
        client = EmbeddingClient()
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT chunk_id,content FROM chunks ORDER BY rowid")]
        updated = 0
        for offset in range(0, len(rows), max(1, batch_size)):
            batch = rows[offset:offset + max(1, batch_size)]
            vectors = client.embed([row["content"] for row in batch])
            with self.connect() as db:
                db.executemany("UPDATE chunks SET vector_json=?,embedding_model=? WHERE chunk_id=?",
                    [(json.dumps(vector), client.identity, row["chunk_id"]) for row, vector in zip(batch, vectors)])
            updated += len(batch)
        return {"updated_chunks": updated, "embedding_model": client.identity}

    def chunks_for(self, company_cik: str, document_type: str | None = None,
                   periods: list[str] | None = None) -> list[dict]:
        clauses, args = ["c.company_cik=?"], [company_cik]
        if document_type:
            clauses.append("c.document_type=?")
            args.append(document_type)
        if periods:
            clauses.append("c.fiscal_period IN (" + ",".join("?" for _ in periods) + ")")
            args.extend(periods)
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT c.*,d.title,d.source_url,d.accession FROM chunks c JOIN documents d USING(doc_id) WHERE " + " AND ".join(clauses), args)]

    def create_run(self, request: dict) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO runs(run_id,created_at,updated_at,status,request_json) VALUES(?,?,?,?,?)",
                       (run_id, utcnow(), utcnow(), "queued", json.dumps(request)))
        return run_id

    def update_run_request(self, run_id: str, request: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET request_json=?,updated_at=? WHERE run_id=?",
                (json.dumps(request), utcnow(), run_id))

    def update_run(self, run_id: str, status: str, result: dict | None = None, error: str | None = None):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,updated_at=?,result_json=COALESCE(?,result_json),error=? WHERE run_id=?",
              (status, utcnow(), json.dumps(result) if result is not None else None, error, run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["request"] = json.loads(result.pop("request_json"))
            result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
            return result

    def trace(self, run_id: str, stage: str, agent: str | None, status: str,
              started: str, completed: str | None = None, input_data=None, output_data=None,
              error: str | None = None, token_usage: int | None = None, metadata: dict | None = None):
        start_dt = datetime.fromisoformat(started)
        end_dt = datetime.fromisoformat(completed) if completed else None
        latency = int((end_dt - start_dt).total_seconds() * 1000) if end_dt else None
        with self.connect() as db:
            db.execute("INSERT INTO traces(trace_id,run_id,stage,agent,status,started_at,completed_at,latency_ms,input_json,output_json,error,token_usage,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, stage, agent, status, started, completed, latency,
                 json.dumps(input_data) if input_data is not None else None,
                 json.dumps(output_data) if output_data is not None else None, error, token_usage,
                 json.dumps(metadata or {})))

    def traces(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM traces WHERE run_id=? ORDER BY started_at,trace_id", (run_id,))]

    def save_evaluation(self, run_id: str | None, dataset_name: str, result: dict):
        with self.connect() as db:
            db.execute("INSERT INTO evaluations(evaluation_id,run_id,created_at,dataset_name,result_json) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, utcnow(), dataset_name, json.dumps(result)))

    def latest_evaluation(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM evaluations ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                return None
            out = dict(row)
            out["result"] = json.loads(out.pop("result_json"))
            return out

