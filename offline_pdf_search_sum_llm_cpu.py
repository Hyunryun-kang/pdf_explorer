# NOTE: 이 버전은 "요약 생성"을 로컬 llama.cpp(gguf)로 수행합니다.
# - 검색/인덱싱/GUI/FTS 구조는 LLM 없는 버전과 동일
# - 모델 파일(.gguf)은 사용자가 준비해야 합니다.

import os
import re
import json
import time
import queue
import math
import shutil
import sqlite3
import threading
import traceback
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import fitz  # pymupdf
except Exception:
    fitz = None

# llama-cpp-python
try:
    from llama_cpp import Llama
except Exception:
    Llama = None  # type: ignore

from concurrent.futures import ProcessPoolExecutor, as_completed

APP_NAME = "오프라인 PDF 내용 검색기 + 요약검색 (로컬 LLM/CPU)"
DB_FILENAME = "offline_pdf_index.db"
SYNONYMS_JSON = "synonyms.json"
SETTINGS_JSON = "settings.json"

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")

def safe_fts_quote(term: str) -> str:
    return term.replace('"', '""').strip()

def normalize_ws(s: str) -> str:
    return " ".join((s or "").replace("\x00", " ").split())

def find_sumatra_exe() -> Optional[str]:
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "SumatraPDF", "SumatraPDF.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "SumatraPDF", "SumatraPDF.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")

def open_file_default(path: str) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        raise RuntimeError(f"파일 열기 실패: {e}")

def open_pdf_at_page(path: str, page_no_1base: int, settings: dict) -> None:
    page_no_1base = max(1, int(page_no_1base))
    mode = (settings.get("viewer_mode") or "").lower().strip()
    vpath = (settings.get("viewer_path") or "").strip()

    def run(cmd: List[str]):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if mode and vpath and os.path.exists(vpath):
        if mode == "sumatra":
            run([vpath, "-reuse-instance", "-page", str(page_no_1base), path]); return
        if mode == "acrobat":
            run([vpath, path, "/A", f"page={page_no_1base}"]); return
        if mode == "custom":
            tmpl = settings.get("viewer_custom_args") or []
            if isinstance(tmpl, list) and tmpl:
                cmd = []
                for tok in tmpl:
                    if isinstance(tok, str):
                        cmd.append(tok.replace("{viewer}", vpath).replace("{file}", path).replace("{page}", str(page_no_1base)))
                if cmd:
                    run(cmd); return

    sumatra = find_sumatra_exe()
    if sumatra:
        run([sumatra, "-reuse-instance", "-page", str(page_no_1base), path]); return

    open_file_default(path)

def char_2grams(text: str) -> str:
    t = normalize_ws(text)
    if not t:
        return ""
    max_chars = 120_000
    if len(t) > max_chars:
        t = t[:max_chars]
    t2 = t.replace(" ", "")
    if len(t2) < 2:
        return t2
    grams = [t2[i:i+2] for i in range(len(t2)-1)]
    if len(grams) > 250_000:
        step = max(1, len(grams)//250_000)
        grams = grams[::step]
    return " ".join(grams)

# -----------------------------
# LLM summarizer (llama.cpp)
# -----------------------------
def llm_summarize(
    llm: "Llama",
    text: str,
    max_chars_in: int = 60_000,
    max_tokens_out: int = 220,
) -> str:
    """
    문서 일부를 넣고 한국어 요약 생성.
    - CPU 환경: 입력을 너무 길게 넣으면 폭발합니다(시간/메모리). 제한 필수.
    """
    t = normalize_ws(text)
    if not t:
        return ""
    if len(t) > max_chars_in:
        t = t[:max_chars_in]

    prompt = (
        "다음 문서를 한국어로 간결하게 요약해라.\n"
        "- 핵심 주제 1~2문장\n"
        "- 주요 포인트 3~6개를 문장으로\n"
        "- 과장/추측 금지, 문서에 있는 내용만\n\n"
        f"문서:\n{t}\n\n요약:\n"
    )

    # llama-cpp-python API: create_completion 또는 __call__
    # 모델에 따라 stop 토큰이 다르니 안전하게 줄바꿈 기반.
    out = llm(
        prompt,
        max_tokens=max_tokens_out,
        temperature=0.2,
        top_p=0.9,
        repeat_penalty=1.1,
        stop=["\n\n\n"],
    )
    txt = ""
    try:
        txt = out["choices"][0]["text"]
    except Exception:
        txt = str(out)
    return normalize_ws(txt)

# -----------------------------
# DB (요약 저장 + 요약 FTS)
# -----------------------------
SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    file_mtime INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    last_indexed_at TEXT,
    status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    page_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
USING fts5(
    text,
    content='pages',
    content_rowid='page_id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, text) VALUES (new.page_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.page_id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.page_id, old.text);
    INSERT INTO pages_fts(rowid, text) VALUES (new.page_id, new.text);
END;

CREATE TABLE IF NOT EXISTS pages_ng (
    page_id INTEGER PRIMARY KEY,
    grams TEXT NOT NULL,
    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_ng_fts
USING fts5(
    grams,
    content='pages_ng',
    content_rowid='page_id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS pagesng_ai AFTER INSERT ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(rowid, grams) VALUES (new.page_id, new.grams);
END;

CREATE TRIGGER IF NOT EXISTS pagesng_ad AFTER DELETE ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(pages_ng_fts, rowid, grams) VALUES ('delete', old.page_id, old.grams);
END;

CREATE TRIGGER IF NOT EXISTS pagesng_au AFTER UPDATE ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(pages_ng_fts, rowid, grams) VALUES ('delete', old.page_id, old.grams);
    INSERT INTO pages_ng_fts(rowid, grams) VALUES (new.page_id, new.grams);
END;

CREATE TABLE IF NOT EXISTS doc_summaries (
    doc_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS doc_summaries_fts
USING fts5(
    summary,
    content='doc_summaries',
    content_rowid='doc_id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS docsumm_ai AFTER INSERT ON doc_summaries BEGIN
    INSERT INTO doc_summaries_fts(rowid, summary) VALUES (new.doc_id, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS docsumm_ad AFTER DELETE ON doc_summaries BEGIN
    INSERT INTO doc_summaries_fts(doc_summaries_fts, rowid, summary) VALUES ('delete', old.doc_id, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS docsumm_au AFTER UPDATE ON doc_summaries BEGIN
    INSERT INTO doc_summaries_fts(doc_summaries_fts, rowid, summary) VALUES ('delete', old.doc_id, old.summary);
    INSERT INTO doc_summaries_fts(rowid, summary) VALUES (new.doc_id, new.summary);
END;

CREATE TABLE IF NOT EXISTS synonyms (
    term TEXT NOT NULL,
    synonym TEXT NOT NULL,
    PRIMARY KEY (term, synonym)
);

CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(doc_id, page_no);
"""

class IndexDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.execute("PRAGMA temp_store = MEMORY;")
        self._conn.execute("PRAGMA cache_size = -200000;")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._conn.execute("SELECT 1 FROM pages_fts LIMIT 1;")
        self._conn.execute("SELECT 1 FROM doc_summaries_fts LIMIT 1;")

    def close(self):
        try: self._conn.close()
        except Exception: pass

    def upsert_document(self, path: str, mtime: int, size: int, status: str = "ok") -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO documents(path, file_mtime, file_size, last_indexed_at, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                file_mtime=excluded.file_mtime,
                file_size=excluded.file_size,
                last_indexed_at=excluded.last_indexed_at,
                status=excluded.status
            """,
            (path, mtime, size, now_iso(), status),
        )
        self._conn.commit()
        cur.execute("SELECT doc_id FROM documents WHERE path = ?", (path,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError("doc_id 조회 실패")
        return int(row["doc_id"])

    def get_document_meta(self, path: str) -> Optional[Tuple[int, int, int]]:
        cur = self._conn.cursor()
        cur.execute("SELECT doc_id, file_mtime, file_size FROM documents WHERE path = ?", (path,))
        row = cur.fetchone()
        if not row:
            return None
        return int(row["doc_id"]), int(row["file_mtime"]), int(row["file_size"])

    def list_document_paths(self) -> Set[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT path FROM documents")
        return {r["path"] for r in cur.fetchall()}

    def delete_document_by_path(self, path: str) -> None:
        self._conn.execute("DELETE FROM documents WHERE path = ?", (path,))
        self._conn.commit()

    def replace_pages(self, doc_id: int, pages_text: List[Tuple[int, str]], build_ngram: bool):
        cur = self._conn.cursor()
        cur.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        cur.executemany(
            "INSERT INTO pages(doc_id, page_no, text) VALUES (?, ?, ?)",
            [(doc_id, pno, txt) for (pno, txt) in pages_text],
        )
        if build_ngram:
            cur.execute("SELECT page_id, text FROM pages WHERE doc_id = ?", (doc_id,))
            grams_rows = [(int(r["page_id"]), char_2grams(r["text"])) for r in cur.fetchall()]
            cur.executemany("INSERT OR REPLACE INTO pages_ng(page_id, grams) VALUES (?, ?)", grams_rows)
        else:
            cur.execute(
                "DELETE FROM pages_ng WHERE page_id IN (SELECT page_id FROM pages WHERE doc_id = ?)",
                (doc_id,),
            )
        self._conn.commit()

    def upsert_doc_summary(self, doc_id: int, summary: str):
        summary = normalize_ws(summary)
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO doc_summaries(doc_id, summary, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                summary=excluded.summary,
                updated_at=excluded.updated_at
            """,
            (doc_id, summary, now_iso()),
        )
        self._conn.commit()

    def load_synonyms_map(self) -> Dict[str, Set[str]]:
        cur = self._conn.cursor()
        cur.execute("SELECT term, synonym FROM synonyms")
        mp: Dict[str, Set[str]] = {}
        for r in cur.fetchall():
            mp.setdefault(r["term"], set()).add(r["synonym"])
        return mp

    def import_synonyms_from_json(self, json_path: str) -> None:
        if not os.path.exists(json_path):
            template = {"예시": ["샘플", "견본"], "PDF": ["피디에프"]}
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            return
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("synonyms.json은 {term:[...]} 형태여야 합니다.")
        rows = []
        for term, syns in data.items():
            if isinstance(term, str) and isinstance(syns, list):
                for s in syns:
                    if isinstance(s, str) and s.strip():
                        rows.append((term.strip(), s.strip()))
        self._conn.executemany("INSERT OR IGNORE INTO synonyms(term, synonym) VALUES (?, ?)", rows)
        self._conn.commit()

    def add_synonym(self, term: str, synonym: str) -> None:
        term, synonym = term.strip(), synonym.strip()
        if not term or not synonym:
            return
        self._conn.execute("INSERT OR IGNORE INTO synonyms(term, synonym) VALUES (?, ?)", (term, synonym))
        self._conn.commit()

    def delete_synonym(self, term: str, synonym: str) -> None:
        self._conn.execute("DELETE FROM synonyms WHERE term = ? AND synonym = ?", (term, synonym))
        self._conn.commit()

    def delete_term(self, term: str) -> None:
        self._conn.execute("DELETE FROM synonyms WHERE term = ?", (term,))
        self._conn.commit()

    def export_synonyms_to_json(self, json_path: str) -> None:
        mp = self.load_synonyms_map()
        data = {k: sorted(list(vs)) for k, vs in sorted(mp.items(), key=lambda x: x[0].lower())}
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def search_pages_text(self, fts_query: str, limit: int = 250) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.path AS path, p.page_no AS page_no,
                   snippet(pages_fts, 0, '[', ']', ' … ', 12) AS snippet,
                   bm25(pages_fts) AS score
            FROM pages_fts
            JOIN pages p ON p.page_id = pages_fts.rowid
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE pages_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, limit),
        )
        return cur.fetchall()

    def search_pages_ngram(self, grams_query: str, limit: int = 250) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.path AS path, p.page_no AS page_no,
                   snippet(pages_fts, 0, '[', ']', ' … ', 12) AS snippet,
                   bm25(pages_ng_fts) AS score
            FROM pages_ng_fts
            JOIN pages p ON p.page_id = pages_ng_fts.rowid
            JOIN pages_fts ON pages_fts.rowid = p.page_id
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE pages_ng_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (grams_query, limit),
        )
        return cur.fetchall()

    def search_doc_summaries(self, fts_query: str, limit: int = 200) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.path AS path,
                   1 AS page_no,
                   snippet(doc_summaries_fts, 0, '[', ']', ' … ', 18) AS snippet,
                   bm25(doc_summaries_fts) AS score
            FROM doc_summaries_fts
            JOIN documents d ON d.doc_id = doc_summaries_fts.rowid
            WHERE doc_summaries_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, limit),
        )
        return cur.fetchall()

# query helpers
def build_graph(mp: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    g: Dict[str, Set[str]] = {}
    for k, vs in mp.items():
        g.setdefault(k, set()).update(vs)
    return g

def expand_terms(base_terms: List[str], graph: Dict[str, Set[str]], max_depth: int = 2) -> Set[str]:
    out: Set[str] = set(t for t in base_terms if t.strip())
    visited: Set[str] = set(out)
    q: List[Tuple[str, int]] = [(t, 0) for t in out]
    while q:
        term, depth = q.pop(0)
        if depth >= max_depth:
            continue
        for nxt in graph.get(term, set()):
            nxt = nxt.strip()
            if not nxt or nxt in visited:
                continue
            visited.add(nxt)
            out.add(nxt)
            q.append((nxt, depth + 1))
    return out

def parse_query(user_query: str) -> Tuple[List[str], List[str]]:
    q = user_query.strip()
    if not q:
        return [], []
    phrases: List[str] = []
    tokens: List[str] = []
    i = 0
    while i < len(q):
        if q[i] == '"':
            j = i + 1
            while j < len(q) and q[j] != '"':
                j += 1
            if j < len(q) and q[j] == '"':
                ph = q[i + 1 : j].strip()
                if ph:
                    phrases.append(ph)
                i = j + 1
            else:
                rest = q[i + 1 :].strip()
                if rest:
                    tokens.extend(rest.split())
                break
        else:
            j = i
            while j < len(q) and q[j] != '"':
                j += 1
            chunk = q[i:j].strip()
            if chunk:
                tokens.extend(chunk.split())
            i = j
    return tokens, phrases

def make_fts_query(user_query: str, expanded: bool, syn_graph: Dict[str, Set[str]]) -> str:
    tokens, phrases = parse_query(user_query)
    parts: List[str] = []
    for ph in phrases:
        parts.append(f'"{safe_fts_quote(ph)}"')
    final_terms: Set[str] = set(tokens)
    if expanded and tokens:
        final_terms = expand_terms(tokens, syn_graph, max_depth=2)
    for t in sorted(final_terms):
        tq = safe_fts_quote(t)
        if tq:
            parts.append(f'"{tq}"')
    return " OR ".join(parts)

def make_ngram_query(user_query: str, expanded: bool, syn_graph: Dict[str, Set[str]]) -> str:
    tokens, phrases = parse_query(user_query)
    all_terms: List[str] = tokens + phrases
    if expanded and tokens:
        exp = expand_terms(tokens, syn_graph, max_depth=2)
        all_terms = list(exp) + phrases
    token_clauses = []
    for term in all_terms:
        term = term.strip()
        if not term:
            continue
        grams = char_2grams(term)
        if not grams:
            continue
        g_tokens = grams.split()
        ands = " AND ".join([f'"{safe_fts_quote(gt)}"' for gt in g_tokens[:80]])
        if ands:
            token_clauses.append(f"({ands})")
    return " OR ".join(token_clauses)

@dataclass
class IndexProgress:
    current: int
    total: int
    path: str

@dataclass
class IndexErrorItem:
    path: str
    error: str

def extract_pdf_text_pages(path: str) -> List[Tuple[int, str]]:
    if fitz is None:
        raise RuntimeError("PyMuPDF 미설치")
    pages_text: List[Tuple[int, str]] = []
    with fitz.open(path) as doc:
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            text = page.get_text("text") or ""
            pages_text.append((pno + 1, normalize_ws(text)))
    return pages_text

def build_doc_text_for_summary(pages_text: List[Tuple[int, str]], max_pages: int = 20) -> str:
    if not pages_text:
        return ""
    parts = []
    for (pno, txt) in pages_text[:max_pages]:
        if txt:
            parts.append(txt)
    return "\n".join(parts)

class IndexerThread(threading.Thread):
    def __init__(
        self,
        db_path: str,
        root_folder: str,
        event_queue: queue.Queue,
        cancel_event: threading.Event,
        build_ngram: bool,
        parallel: bool,
        max_workers: int,
        build_summary: bool,
        llm_model_path: str,
        llm_ctx: int,
        llm_threads: int,
    ):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.root_folder = root_folder
        self.q = event_queue
        self.cancel = cancel_event
        self.build_ngram = build_ngram
        self.parallel = parallel
        self.max_workers = max_workers
        self.build_summary = build_summary
        self.llm_model_path = llm_model_path
        self.llm_ctx = llm_ctx
        self.llm_threads = llm_threads
        self._llm = None

    def _get_llm(self):
        if not self.build_summary:
            return None
        if Llama is None:
            raise RuntimeError("llama-cpp-python이 설치되어 있지 않습니다. pip install llama-cpp-python")
        if not self.llm_model_path or not os.path.exists(self.llm_model_path):
            raise RuntimeError("LLM 모델(.gguf) 경로가 유효하지 않습니다. 인덱싱 옵션에서 모델 파일을 지정하세요.")
        if self._llm is None:
            # n_gpu_layers=0 => CPU
            self._llm = Llama(
                model_path=self.llm_model_path,
                n_ctx=int(self.llm_ctx),
                n_threads=max(1, int(self.llm_threads)),
                n_gpu_layers=0,
                verbose=False,
            )
        return self._llm

    def run(self):
        if fitz is None:
            self.q.put(("fatal", "PyMuPDF(pymupdf)가 필요합니다: pip install pymupdf")); return

        db = None
        try:
            db = IndexDB(self.db_path)
            self._index(db)
            self.q.put(("done", None))
        except Exception as e:
            self.q.put(("fatal", f"{e}\n\n{traceback.format_exc()}"))
        finally:
            if db:
                db.close()

    def _collect_pdfs(self) -> List[str]:
        out: List[str] = []
        for base, _, files in os.walk(self.root_folder):
            if self.cancel.is_set():
                break
            for fn in files:
                if is_pdf(fn):
                    out.append(os.path.join(base, fn))
        return out

    def _index(self, db: IndexDB):
        pdfs = self._collect_pdfs()
        total = len(pdfs)
        self.q.put(("scan", total))

        existing = db.list_document_paths()
        current_set = set(pdfs)
        deleted = sorted(existing - current_set)
        for path in deleted:
            if self.cancel.is_set():
                self.q.put(("canceled", None)); return
            db.delete_document_by_path(path)
            self.q.put(("deleted", path))

        to_process: List[str] = []
        metas: Dict[str, Tuple[int, int]] = {}
        for path in pdfs:
            if self.cancel.is_set():
                self.q.put(("canceled", None)); return
            try:
                st = os.stat(path)
                mtime = int(st.st_mtime)
                size = int(st.st_size)
                metas[path] = (mtime, size)
                meta = db.get_document_meta(path)
                needs = True
                if meta:
                    _, old_mtime, old_size = meta
                    if old_mtime == mtime and old_size == size:
                        needs = False
                if needs:
                    to_process.append(path)
            except Exception:
                to_process.append(path)

        self.q.put(("plan", (len(to_process), total)))

        errors: List[IndexErrorItem] = []

        # LLM은 인덱싱 스레드에서만 사용(프로세스 풀과 섞지 않음)
        llm = self._get_llm() if self.build_summary else None

        if not self.parallel or len(to_process) <= 1:
            for idx, path in enumerate(pdfs, start=1):
                if self.cancel.is_set():
                    self.q.put(("canceled", None)); return
                self.q.put(("progress", IndexProgress(idx, total, path)))
                if path not in to_process:
                    continue
                try:
                    mtime, size = metas.get(path, (int(os.stat(path).st_mtime), int(os.stat(path).st_size)))
                    doc_id = db.upsert_document(path, mtime, size, status="ok")
                    pages_text = extract_pdf_text_pages(path)
                    db.replace_pages(doc_id, pages_text, build_ngram=self.build_ngram)

                    if self.build_summary and llm is not None:
                        doc_text = build_doc_text_for_summary(pages_text)
                        self.q.put(("llm", f"요약 생성 중: {os.path.basename(path)}"))
                        summ = llm_summarize(llm, doc_text, max_chars_in=60_000, max_tokens_out=240)
                        db.upsert_doc_summary(doc_id, summ)

                    db.upsert_document(path, mtime, size, status="ok")
                except Exception as e:
                    errors.append(IndexErrorItem(path, str(e)))
                    try:
                        st2 = os.stat(path)
                        db.upsert_document(path, int(st2.st_mtime), int(st2.st_size), status="error")
                    except Exception:
                        pass
                    self.q.put(("error", errors[-1]))
        else:
            workers = max(1, min(self.max_workers, os.cpu_count() or 2))
            self.q.put(("parallel", workers))
            futures = {}
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for path in to_process:
                    if self.cancel.is_set():
                        self.q.put(("canceled", None)); return
                    futures[ex.submit(extract_pdf_text_pages, path)] = path

                done_count = 0
                for fut in as_completed(futures):
                    if self.cancel.is_set():
                        self.q.put(("canceled", None)); return
                    path = futures[fut]
                    done_count += 1
                    self.q.put(("extract_done", (done_count, len(to_process), path)))
                    try:
                        pages_text = fut.result()
                        mtime, size = metas.get(path, (int(os.stat(path).st_mtime), int(os.stat(path).st_size)))
                        doc_id = db.upsert_document(path, mtime, size, status="ok")
                        db.replace_pages(doc_id, pages_text, build_ngram=self.build_ngram)

                        if self.build_summary and llm is not None:
                            doc_text = build_doc_text_for_summary(pages_text)
                            self.q.put(("llm", f"요약 생성 중: {os.path.basename(path)}"))
                            summ = llm_summarize(llm, doc_text, max_chars_in=60_000, max_tokens_out=240)
                            db.upsert_doc_summary(doc_id, summ)

                        db.upsert_document(path, mtime, size, status="ok")
                    except Exception as e:
                        errors.append(IndexErrorItem(path, str(e)))
                        try:
                            st2 = os.stat(path)
                            db.upsert_document(path, int(st2.st_mtime), int(st2.st_size), status="error")
                        except Exception:
                            pass
                        self.q.put(("error", errors[-1]))

        self.q.put(("errors_done", errors))

# -----------------------------
# Synonyms dialog (same as no-llm but included)
# -----------------------------
class SynonymsDialog(tk.Toplevel):
    def __init__(self, parent, db_path: str, on_changed_callback):
        super().__init__(parent)
        self.title("동의어 관리")
        self.geometry("900x560")
        self.minsize(800, 520)
        self.db_path = db_path
        self.on_changed_callback = on_changed_callback
        self.term_var = tk.StringVar(value="")
        self.syn_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value="")
        self._build()
        self._reload()

    def _build(self):
        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        ttk.Label(top, text="필터:").pack(side="left")
        ttk.Entry(top, textvariable=self.filter_var, width=30).pack(side="left", padx=6)
        ttk.Button(top, text="적용", command=self._reload).pack(side="left")
        ttk.Button(top, text="synonyms.json 내보내기", command=self._export).pack(side="right", padx=4)
        ttk.Button(top, text="synonyms.json 가져오기", command=self._import).pack(side="right", padx=4)

        mid = ttk.Frame(self, padding=(10, 0, 10, 10)); mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("term", "syn"), show="headings")
        self.tree.heading("term", text="term")
        self.tree.heading("syn", text="synonym")
        self.tree.column("term", width=300, anchor="w")
        self.tree.column("syn", width=540, anchor="w")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        bottom = ttk.LabelFrame(self, text="추가/삭제", padding=10); bottom.pack(fill="x", padx=10, pady=(0, 10))
        row1 = ttk.Frame(bottom); row1.pack(fill="x")
        ttk.Label(row1, text="term").pack(side="left")
        ttk.Entry(row1, textvariable=self.term_var, width=30).pack(side="left", padx=6)
        ttk.Label(row1, text="synonym").pack(side="left")
        ttk.Entry(row1, textvariable=self.syn_var, width=45).pack(side="left", padx=6)
        ttk.Button(row1, text="추가", command=self._add).pack(side="left", padx=6)

        row2 = ttk.Frame(bottom); row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="선택 항목 삭제", command=self._delete_selected).pack(side="left")
        ttk.Button(row2, text="선택 term 전체 삭제", command=self._delete_term).pack(side="left", padx=6)

        self.status = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w", pady=(8, 0))

    def _reload(self):
        for it in self.tree.get_children():
            self.tree.delete(it)
        flt = self.filter_var.get().strip().lower()
        db = IndexDB(self.db_path)
        mp = db.load_synonyms_map()
        db.close()
        rows = []
        for term, syns in mp.items():
            for s in syns:
                if flt and (flt not in term.lower() and flt not in s.lower()):
                    continue
                rows.append((term, s))
        rows.sort(key=lambda x: (x[0].lower(), x[1].lower()))
        for term, s in rows:
            self.tree.insert("", "end", values=(term, s))
        self.status.set(f"총 {len(rows)}개 항목 표시 중")

    def _add(self):
        term = self.term_var.get().strip()
        syn = self.syn_var.get().strip()
        if not term or not syn:
            messagebox.showwarning("값 필요", "term과 synonym을 입력하세요.")
            return
        db = IndexDB(self.db_path)
        db.add_synonym(term, syn)
        db.close()
        self.term_var.set(""); self.syn_var.set("")
        self._reload()
        self.on_changed_callback()

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        term, syn = self.tree.item(sel[0], "values")
        db = IndexDB(self.db_path)
        db.delete_synonym(term, syn)
        db.close()
        self._reload()
        self.on_changed_callback()

    def _delete_term(self):
        sel = self.tree.selection()
        if not sel:
            return
        term, _ = self.tree.item(sel[0], "values")
        if messagebox.askyesno("확인", f"term '{term}'의 모든 synonym을 삭제할까요?"):
            db = IndexDB(self.db_path)
            db.delete_term(term)
            db.close()
            self._reload()
            self.on_changed_callback()

    def _export(self):
        try:
            db = IndexDB(self.db_path)
            db.export_synonyms_to_json(SYNONYMS_JSON)
            db.close()
            messagebox.showinfo("완료", f"{SYNONYMS_JSON}로 내보냈습니다.")
        except Exception as e:
            messagebox.showerror("실패", str(e))

    def _import(self):
        try:
            db = IndexDB(self.db_path)
            db.import_synonyms_from_json(SYNONYMS_JSON)
            db.close()
            messagebox.showinfo("완료", f"{SYNONYMS_JSON}에서 가져왔습니다.")
            self._reload()
            self.on_changed_callback()
        except Exception as e:
            messagebox.showerror("실패", str(e))

# -----------------------------
# App
# -----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1220x840")
        self.minsize(980, 660)

        self.db_path = os.path.join(os.getcwd(), DB_FILENAME)
        self.event_q: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.indexer: Optional[IndexerThread] = None

        self.syn_graph: Dict[str, Set[str]] = {}
        self.settings = self._load_settings()

        self._build_ui()
        self._ensure_db()
        self._load_synonyms()
        self._poll_events()

    def _load_settings(self) -> dict:
        if not os.path.exists(SETTINGS_JSON):
            s = {
                "viewer_mode": "",
                "viewer_path": "",
                "viewer_custom_args": ["{viewer}", "{file}"],
                "llm_model_path": "",
                "llm_ctx": 2048,
                "llm_threads": max(1, (os.cpu_count() or 4) - 1),
            }
            with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            return s
        try:
            with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_settings(self):
        try:
            with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showwarning("설정 저장 실패", str(e))

    def _ensure_db(self):
        try:
            db = IndexDB(self.db_path); db.close()
        except Exception as e:
            messagebox.showerror("DB 초기화 실패", str(e))
            self.destroy()

    def _load_synonyms(self):
        try:
            db = IndexDB(self.db_path)
            db.import_synonyms_from_json(SYNONYMS_JSON)
            mp = db.load_synonyms_map()
            db.close()
            self.syn_graph = build_graph(mp)
            self.syn_status_var.set(f"동의어: {len(mp)}개 term 로드됨")
        except Exception as e:
            self.syn_graph = {}
            self.syn_status_var.set(f"동의어 로드 실패: {e}")

    def _build_ui(self):
        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        self.folder_var = tk.StringVar(value="")
        ttk.Label(top, text="대상 폴더:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder_var, width=62).pack(side="left", padx=6)
        ttk.Button(top, text="폴더 선택", command=self.on_pick_folder).pack(side="left", padx=4)
        ttk.Button(top, text="인덱싱 시작", command=self.on_start_index).pack(side="left", padx=4)
        ttk.Button(top, text="취소", command=self.on_cancel_index).pack(side="left", padx=4)
        ttk.Button(top, text="동의어 관리", command=self.on_manage_synonyms).pack(side="left", padx=8)
        ttk.Button(top, text="뷰어/LLM 설정", command=self.on_settings).pack(side="left", padx=4)

        prog = ttk.Frame(self, padding=(10, 0, 10, 10)); prog.pack(fill="x")
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(prog, variable=self.progress_var, maximum=100).pack(fill="x")
        self.status_var = tk.StringVar(value="준비됨. 폴더 선택 → 인덱싱.")
        ttk.Label(prog, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))
        self.syn_status_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.syn_status_var).pack(anchor="w", pady=(2, 0))

        opt = ttk.LabelFrame(self, text="인덱싱 옵션", padding=10)
        opt.pack(fill="x", padx=10, pady=(0, 10))

        self.build_ngram_var = tk.BooleanVar(value=False)
        self.parallel_var = tk.BooleanVar(value=False)
        self.workers_var = tk.IntVar(value=min(4, os.cpu_count() or 2))
        self.build_summary_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(opt, text="한국어 n-gram(2-gram) 보조 인덱스 생성", variable=self.build_ngram_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opt, text="병렬 PDF 추출", variable=self.parallel_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="workers:").grid(row=1, column=1, sticky="e", padx=(12, 2))
        ttk.Spinbox(opt, from_=1, to=max(1, os.cpu_count() or 2), textvariable=self.workers_var, width=6).grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(opt, text="문서 요약 생성/갱신(로컬 LLM)", variable=self.build_summary_var).grid(row=2, column=0, sticky="w", pady=(6, 0))

        search = ttk.LabelFrame(self, text="검색", padding=10)
        search.pack(fill="x", padx=10, pady=(0, 10))
        self.query_var = tk.StringVar(value="")
        ttk.Label(search, text='검색어("따옴표" 구문 가능):').grid(row=0, column=0, sticky="w")
        ttk.Entry(search, textvariable=self.query_var, width=72).grid(row=0, column=1, sticky="w", padx=6)

        self.expand_var = tk.BooleanVar(value=False)
        self.ngram_search_var = tk.BooleanVar(value=False)
        self.summary_search_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(search, text="동의어 확장", variable=self.expand_var).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Checkbutton(search, text="n-gram 보조 검색", variable=self.ngram_search_var).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Checkbutton(search, text="요약(문서) 검색", variable=self.summary_search_var).grid(row=0, column=4, sticky="w", padx=6)
        ttk.Button(search, text="검색", command=self.on_search).grid(row=0, column=5, sticky="w", padx=6)

        mid = ttk.Frame(self, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)
        cols = ("path", "page", "snippet", "src")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        for c, t, w in [("path","파일 경로",560), ("page","페이지",70), ("snippet","스니펫",480), ("src","출처",70)]:
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w" if c!="page" and c!="src" else "center")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.grid_rowconfigure(0, weight=1); mid.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_open_selected)

        bottom = ttk.LabelFrame(self, text="오류 목록/상태", padding=10)
        bottom.pack(fill="both", padx=10, pady=(0, 10))
        self.err_text = tk.Text(bottom, height=7, wrap="word")
        self.err_text.pack(fill="both", expand=True)

    def on_pick_folder(self):
        folder = filedialog.askdirectory(title="PDF 폴더 선택")
        if folder:
            self.folder_var.set(folder)

    def on_manage_synonyms(self):
        SynonymsDialog(self, self.db_path, on_changed_callback=self._load_synonyms)

    def on_settings(self):
        win = tk.Toplevel(self)
        win.title("뷰어/LLM 설정")
        win.geometry("820x420")
        win.minsize(760, 380)

        mode_var = tk.StringVar(value=(self.settings.get("viewer_mode") or ""))
        path_var = tk.StringVar(value=(self.settings.get("viewer_path") or ""))
        custom_args_var = tk.StringVar(value=json.dumps(self.settings.get("viewer_custom_args") or ["{viewer}", "{file}"], ensure_ascii=False))

        llm_path_var = tk.StringVar(value=(self.settings.get("llm_model_path") or ""))
        llm_ctx_var = tk.IntVar(value=int(self.settings.get("llm_ctx") or 2048))
        llm_threads_var = tk.IntVar(value=int(self.settings.get("llm_threads") or max(1,(os.cpu_count() or 4)-1)))

        frm = ttk.Frame(win, padding=12); frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="PDF 뷰어 모드:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=mode_var, values=["", "sumatra", "acrobat", "custom"], state="readonly", width=14)\
            .grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="(빈값: Sumatra 자동 → 기본 열기)").grid(row=0, column=2, sticky="w")

        ttk.Label(frm, text="뷰어 exe 경로:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=path_var, width=76).grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(10, 0))
        def pick_exe():
            p = filedialog.askopenfilename(title="뷰어 exe 선택", filetypes=[("EXE", "*.exe"), ("All", "*.*")])
            if p: path_var.set(p)
        ttk.Button(frm, text="찾기", command=pick_exe).grid(row=1, column=3, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="custom args(JSON list):").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=custom_args_var, width=76).grid(row=2, column=1, columnspan=3, sticky="w", padx=6, pady=(10, 0))

        ttk.Separator(frm).grid(row=3, column=0, columnspan=4, sticky="ew", pady=12)

        ttk.Label(frm, text="LLM 모델(.gguf) 경로:").grid(row=4, column=0, sticky="w")
        ttk.Entry(frm, textvariable=llm_path_var, width=76).grid(row=4, column=1, columnspan=2, sticky="w", padx=6)
        def pick_gguf():
            p = filedialog.askopenfilename(title="GGUF 모델 선택", filetypes=[("GGUF", "*.gguf"), ("All", "*.*")])
            if p: llm_path_var.set(p)
        ttk.Button(frm, text="찾기", command=pick_gguf).grid(row=4, column=3, sticky="w")

        ttk.Label(frm, text="컨텍스트(n_ctx):").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(frm, from_=512, to=8192, textvariable=llm_ctx_var, width=8).grid(row=5, column=1, sticky="w", padx=6, pady=(10, 0))
        ttk.Label(frm, text="스레드(n_threads):").grid(row=5, column=2, sticky="e", pady=(10, 0))
        ttk.Spinbox(frm, from_=1, to=max(1, os.cpu_count() or 2), textvariable=llm_threads_var, width=8).grid(row=5, column=3, sticky="w", pady=(10, 0))

        def save():
            self.settings["viewer_mode"] = mode_var.get().strip()
            self.settings["viewer_path"] = path_var.get().strip()
            try:
                args = json.loads(custom_args_var.get().strip())
                if not isinstance(args, list):
                    raise ValueError
                self.settings["viewer_custom_args"] = args
            except Exception:
                messagebox.showwarning("형식 오류", "custom args는 JSON 배열이어야 합니다.")
                return

            self.settings["llm_model_path"] = llm_path_var.get().strip()
            self.settings["llm_ctx"] = int(llm_ctx_var.get())
            self.settings["llm_threads"] = int(llm_threads_var.get())
            self._save_settings()
            messagebox.showinfo("저장", "설정을 저장했습니다.")
            win.destroy()

        ttk.Button(frm, text="저장", command=save).grid(row=6, column=3, sticky="e", pady=(16, 0))

    def on_start_index(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("폴더 필요", "유효한 폴더를 선택하세요.")
            return
        if self.indexer and self.indexer.is_alive():
            messagebox.showinfo("이미 실행 중", "인덱싱이 이미 실행 중입니다.")
            return

        self.err_text.delete("1.0", "end")
        self.progress_var.set(0)
        self.status_var.set("스캔 중…")
        self.cancel_event.clear()

        self.indexer = IndexerThread(
            db_path=self.db_path,
            root_folder=folder,
            event_queue=self.event_q,
            cancel_event=self.cancel_event,
            build_ngram=self.build_ngram_var.get(),
            parallel=self.parallel_var.get(),
            max_workers=int(self.workers_var.get()),
            build_summary=self.build_summary_var.get(),
            llm_model_path=str(self.settings.get("llm_model_path") or ""),
            llm_ctx=int(self.settings.get("llm_ctx") or 2048),
            llm_threads=int(self.settings.get("llm_threads") or max(1,(os.cpu_count() or 4)-1)),
        )
        self.indexer.start()

    def on_cancel_index(self):
        if self.indexer and self.indexer.is_alive():
            self.cancel_event.set()
            self.status_var.set("취소 요청됨…")
        else:
            self.status_var.set("실행 중 인덱싱 없음")

    def on_search(self):
        user_q = self.query_var.get().strip()
        if not user_q:
            messagebox.showinfo("검색어 없음", "검색어를 입력하세요.")
            return

        try:
            db = IndexDB(self.db_path)
            fts_q = make_fts_query(user_q, self.expand_var.get(), self.syn_graph)

            merged: Dict[Tuple[str, int, str], Tuple[str, float]] = {}

            if fts_q:
                rows = db.search_pages_text(fts_q, limit=250)
                for r in rows:
                    merged[(r["path"], int(r["page_no"]), "page")] = (r["snippet"] or "", float(r["score"]))

            if self.ngram_search_var.get():
                ng_q = make_ngram_query(user_q, self.expand_var.get(), self.syn_graph)
                if ng_q:
                    rows = db.search_pages_ngram(ng_q, limit=250)
                    for r in rows:
                        key = (r["path"], int(r["page_no"]), "ng")
                        if key not in merged:
                            merged[key] = (r["snippet"] or "", float(r["score"]) + 2.0)

            if self.summary_search_var.get() and fts_q:
                rows = db.search_doc_summaries(fts_q, limit=200)
                for r in rows:
                    key = (r["path"], 1, "summary")
                    if key not in merged:
                        merged[key] = (r["snippet"] or "", float(r["score"]) + 1.0)

            db.close()

            items = sorted(merged.items(), key=lambda kv: kv[1][1])

            for it in self.tree.get_children():
                self.tree.delete(it)
            for (path, page, src), (snip, _sc) in items[:600]:
                self.tree.insert("", "end", values=(path, page, snip, src))

            self.status_var.set(f"검색 완료: {len(items)}건 | 쿼리: {fts_q}")

        except Exception as e:
            messagebox.showerror("검색 실패", str(e))

    def on_open_selected(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not vals:
            return
        path = vals[0]
        page = int(vals[1]) if len(vals) > 1 else 1
        try:
            open_pdf_at_page(path, page, self.settings)
        except Exception as e:
            messagebox.showerror("열기 실패", str(e))

    def _poll_events(self):
        try:
            while True:
                typ, payload = self.event_q.get_nowait()
                if typ == "scan":
                    total = int(payload)
                    self.progress_var.set(0)
                    self.status_var.set(f"PDF 스캔 완료. 총 {total}개")
                elif typ == "plan":
                    changed, total = payload
                    self.status_var.set(f"변경/신규 {changed}개 / 총 {total}개 처리 중…")
                elif typ == "parallel":
                    self.status_var.set(f"병렬 추출 모드: workers={payload}")
                elif typ == "llm":
                    self.status_var.set(str(payload))
                elif typ == "progress":
                    p: IndexProgress = payload
                    self.progress_var.set((p.current / max(1, p.total)) * 100.0)
                    self.status_var.set(f"[{p.current}/{p.total}] 인덱싱: {p.path}")
                elif typ == "extract_done":
                    done, total_changed, path = payload
                    self.progress_var.set((done / max(1, total_changed)) * 100.0)
                    self.status_var.set(f"[{done}/{total_changed}] 추출완료→DB/요약: {path}")
                elif typ == "deleted":
                    self.status_var.set(f"삭제/이동 반영: {payload}")
                elif typ == "error":
                    err: IndexErrorItem = payload
                    self.err_text.insert("end", f"- {err.path}\n  {err.error}\n")
                    self.err_text.see("end")
                elif typ == "errors_done":
                    errs = payload
                    self.progress_var.set(100.0)
                    self.status_var.set(f"인덱싱 완료(오류 {len(errs)}건)" if errs else "인덱싱 완료(오류 없음)")
                elif typ == "canceled":
                    self.status_var.set("인덱싱 취소됨")
                elif typ == "done":
                    self.status_var.set("인덱싱 완료. 검색 가능.")
                    self.progress_var.set(100.0)
                elif typ == "fatal":
                    messagebox.showerror("치명적 오류", str(payload))
                    self.status_var.set("오류로 중단됨")
        except queue.Empty:
            pass
        finally:
            self.after(120, self._poll_events)

def main():
    if fitz is None:
        print("PyMuPDF가 필요합니다: pip install pymupdf")
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
