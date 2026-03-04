import os
import sys
import time
import json
import queue
import shutil
import sqlite3
import threading
import traceback
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from heapq import nsmallest
from typing import Dict, List, Set, Tuple, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# PDF text extraction (PyMuPDF)
try:
    import fitz  # pymupdf
except Exception:
    fitz = None

from concurrent.futures import ProcessPoolExecutor, as_completed


APP_NAME = "오프라인 PDF 내용 검색기 (최적화)"
DB_FILENAME = "offline_pdf_index.db"
SYNONYMS_JSON = "synonyms.json"
SETTINGS_JSON = "settings.json"


# -----------------------------
# Utility
# -----------------------------
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")


def safe_fts_quote(term: str) -> str:
    return term.replace('"', '""').strip()


def normalize_ws(s: str) -> str:
    return " ".join((s or "").replace("\x00", " ").split())


def char_2grams(text: str) -> str:
    """
    한국어 포함 텍스트의 문자 2-gram을 공백으로 연결한 문자열로 반환.
    - 제너레이터 방식으로 중간 리스트 생성 없이 메모리 절감.
    - 너무 긴 문서는 인덱스가 무거워질 수 있으니 최대 길이 제한.
    """
    t = normalize_ws(text)[:120_000]
    if not t:
        return ""

    # 공백 제거(단어 경계는 약해지지만 n-gram 목적상 OK)
    t2 = t.replace(" ", "")
    if len(t2) < 2:
        return t2

    gram_count = len(t2) - 1
    # 250_000 초과 시 샘플링 step 계산 후 제너레이터로 직접 join (중간 리스트 불필요)
    step = max(1, gram_count // 250_000) if gram_count > 250_000 else 1
    return " ".join(t2[i:i+2] for i in range(0, gram_count, step))


def open_file_default(path: str) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        raise RuntimeError(f"파일 열기 실패: {e}")


def find_sumatra_exe() -> Optional[str]:
    # 흔한 설치 위치들
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "SumatraPDF", "SumatraPDF.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "SumatraPDF", "SumatraPDF.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # PATH에 있다면
    exe = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    return exe


def open_pdf_at_page(path: str, page_no_1base: int, settings: dict) -> None:
    """
    '페이지 점프'는 뷰어마다 지원이 다르니 best-effort.
    우선순위:
    1) settings에 viewer_mode + viewer_path 지정되어 있으면 해당 모드로 실행
    2) SumatraPDF가 있으면 Sumatra 방식 (-page)
    3) Adobe Reader(설정이 있으면) /A "page=..."
    4) 그 외: 기본 열기
    """
    page_no_1base = max(1, int(page_no_1base))

    mode = (settings.get("viewer_mode") or "").lower().strip()
    vpath = (settings.get("viewer_path") or "").strip()

    def run(cmd: List[str]):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 1) user-configured
    if mode and vpath and os.path.exists(vpath):
        if mode == "sumatra":
            run([vpath, "-reuse-instance", "-page", str(page_no_1base), path])
            return
        if mode == "acrobat":
            # Acrobat/Reader: AcroRd32.exe "file.pdf" /A "page=3"
            run([vpath, path, "/A", f"page={page_no_1base}"])
            return
        if mode == "custom":
            # custom args template: e.g. ["{viewer}", "-page", "{page}", "{file}"]
            tmpl = settings.get("viewer_custom_args") or []
            if isinstance(tmpl, list) and tmpl:
                cmd = []
                for tok in tmpl:
                    if not isinstance(tok, str):
                        continue
                    cmd.append(
                        tok.replace("{viewer}", vpath)
                        .replace("{file}", path)
                        .replace("{page}", str(page_no_1base))
                    )
                if cmd:
                    run(cmd)
                    return

    # 2) auto Sumatra
    sumatra = find_sumatra_exe()
    if sumatra:
        run([sumatra, "-reuse-instance", "-page", str(page_no_1base), path])
        return

    # 3) fallback: default open
    open_file_default(path)


# -----------------------------
# DB Layer
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

-- N-gram(2-gram) 보조 인덱스 (옵션)
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

CREATE TABLE IF NOT EXISTS synonyms (
    term TEXT NOT NULL,
    synonym TEXT NOT NULL,
    PRIMARY KEY (term, synonym)
);

CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path);
CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(doc_id);
"""


class IndexDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        # 성능 설정
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.execute("PRAGMA temp_store = MEMORY;")
        self._conn.execute("PRAGMA cache_size = -200000;")  # ~200MB
        self._conn.execute("PRAGMA mmap_size = 67108864;")  # 64MB 메모리 맵 I/O

        self._init_schema()

        # FTS5 availability check
        try:
            self._conn.execute("SELECT 1 FROM pages_fts LIMIT 1;")
            self._conn.execute("SELECT 1 FROM pages_ng_fts LIMIT 1;")
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                "SQLite FTS5를 사용할 수 없습니다. "
                "현재 Python/SQLite 빌드에서 FTS5가 비활성화된 것 같습니다.\n"
                f"원인: {e}"
            )

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def _init_schema(self):
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

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
            raise RuntimeError("문서 upsert 후 doc_id 조회 실패")
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
        cur = self._conn.cursor()
        cur.execute("DELETE FROM documents WHERE path = ?", (path,))
        self._conn.commit()

    def replace_pages(self, doc_id: int, pages_text: List[Tuple[int, str]], build_ngram: bool):
        """
        doc_id의 기존 페이지/그램 제거 후 재삽입.
        """
        cur = self._conn.cursor()
        cur.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        # pages insert
        cur.executemany(
            "INSERT INTO pages(doc_id, page_no, text) VALUES (?, ?, ?)",
            [(doc_id, pno, txt) for (pno, txt) in pages_text],
        )

        if build_ngram:
            # page_id만 재조회(텍스트는 이미 pages_text에 있으므로 재전송 불필요)
            pno_to_text = {pno: txt for pno, txt in pages_text}
            cur.execute("SELECT page_id, page_no FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,))
            grams_rows = [
                (int(r["page_id"]), char_2grams(pno_to_text.get(r["page_no"], "")))
                for r in cur.fetchall()
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO pages_ng(page_id, grams) VALUES (?, ?)",
                grams_rows,
            )
        else:
            # ngram 끄면 doc_id의 pages_ng도 제거(FTS도 트리거로 정리)
            cur.execute(
                "DELETE FROM pages_ng WHERE page_id IN (SELECT page_id FROM pages WHERE doc_id = ?)",
                (doc_id,),
            )

        self._conn.commit()

    # ---- synonyms ----
    def load_synonyms_map(self) -> Dict[str, Set[str]]:
        cur = self._conn.cursor()
        cur.execute("SELECT term, synonym FROM synonyms")
        mp: Dict[str, Set[str]] = {}
        for r in cur.fetchall():
            mp.setdefault(r["term"], set()).add(r["synonym"])
        return mp

    def add_synonym(self, term: str, synonym: str) -> None:
        term = term.strip()
        synonym = synonym.strip()
        if not term or not synonym:
            return
        cur = self._conn.cursor()
        cur.execute("INSERT OR IGNORE INTO synonyms(term, synonym) VALUES (?, ?)", (term, synonym))
        self._conn.commit()

    def delete_synonym(self, term: str, synonym: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM synonyms WHERE term = ? AND synonym = ?", (term, synonym))
        self._conn.commit()

    def delete_term(self, term: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM synonyms WHERE term = ?", (term,))
        self._conn.commit()

    def export_synonyms_to_json(self, json_path: str) -> None:
        mp = self.load_synonyms_map()
        data = {k: sorted(list(vs)) for k, vs in sorted(mp.items(), key=lambda x: x[0].lower())}
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_synonyms_from_json(self, json_path: str) -> None:
        if not os.path.exists(json_path):
            template = {"예시": ["샘플", "견본"], "PDF": ["피디에프"]}
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            return

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("synonyms.json은 {term: [syn1, syn2]} 형태의 JSON 이어야 합니다.")

        rows = []
        for term, syns in data.items():
            if not isinstance(term, str) or not isinstance(syns, list):
                continue
            for s in syns:
                if isinstance(s, str) and s.strip():
                    rows.append((term.strip(), s.strip()))

        cur = self._conn.cursor()
        cur.executemany("INSERT OR IGNORE INTO synonyms(term, synonym) VALUES (?, ?)", rows)
        self._conn.commit()

    # ---- search ----
    def search_text(self, fts_query: str, limit: int = 250) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT
                d.path AS path,
                p.page_no AS page_no,
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

    def search_ngram(self, grams_query: str, limit: int = 250) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT
                d.path AS path,
                p.page_no AS page_no,
                snippet(pages_ng_fts, 0, '[', ']', ' … ', 12) AS snippet,
                bm25(pages_ng_fts) AS score
            FROM pages_ng_fts
            JOIN pages p ON p.page_id = pages_ng_fts.rowid
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE pages_ng_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (grams_query, limit),
        )
        return cur.fetchall()


# -----------------------------
# Synonym expansion
# -----------------------------
def build_graph(mp: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    g: Dict[str, Set[str]] = {}
    for k, vs in mp.items():
        g.setdefault(k, set()).update(vs)
    return g


def expand_terms(base_terms: List[str], graph: Dict[str, Set[str]], max_depth: int = 2) -> Set[str]:
    out: Set[str] = {t for t in base_terms if t.strip()}
    visited: Set[str] = set(out)
    # deque 사용으로 popleft() O(1) (list.pop(0)는 O(n))
    q: deque = deque((t, 0) for t in out)
    while q:
        term, depth = q.popleft()
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
    """
    사용자 토큰을 2-gram으로 바꿔 MATCH 쿼리 생성.
    - 토큰 각각의 2-gram들을 AND로 묶고, 토큰 간 OR로 묶는 방식.
    - 예: "동탄 신도시" -> ("동탄" grams AND) OR ("신도시" grams AND)
    """
    tokens, phrases = parse_query(user_query)
    # phrase는 일단 그대로도 처리하되, phrase도 2-gram으로 (노이즈 크면 지우기)
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
        # term에 대한 grams는 AND로
        ands = " AND ".join([f'"{safe_fts_quote(gt)}"' for gt in g_tokens[:80]])  # 과도한 AND 제한
        if ands:
            token_clauses.append(f"({ands})")

    return " OR ".join(token_clauses)


# -----------------------------
# Indexing (single-process or parallel)
# -----------------------------
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
    """
    프로세스 작업 단위: PDF 하나에서 페이지별 텍스트 추출
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF 미설치")
    pages_text: List[Tuple[int, str]] = []
    with fitz.open(path) as doc:
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            text = page.get_text("text") or ""
            pages_text.append((pno + 1, normalize_ws(text)))
    return pages_text


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
    ):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.root_folder = root_folder
        self.q = event_queue
        self.cancel = cancel_event
        self.build_ngram = build_ngram
        self.parallel = parallel
        self.max_workers = max_workers

    def run(self):
        if fitz is None:
            self.q.put(("fatal", "PyMuPDF(pymupdf)가 설치되어 있지 않습니다. `pip install pymupdf` 후 다시 실행하세요."))
            return

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
                if fn.lower().endswith(".pdf"):
                    out.append(os.path.join(base, fn))
        return out

    def _index(self, db: IndexDB):
        pdfs = self._collect_pdfs()
        total = len(pdfs)
        self.q.put(("scan", total))

        # 삭제 반영
        existing = db.list_document_paths()
        current_set = set(pdfs)
        deleted = sorted(existing - current_set)
        for path in deleted:
            if self.cancel.is_set():
                self.q.put(("canceled", None))
                return
            db.delete_document_by_path(path)
            self.q.put(("deleted", path))

        # 변경 대상만 추리기
        to_process: List[str] = []
        metas: Dict[str, Tuple[int, int]] = {}
        for path in pdfs:
            if self.cancel.is_set():
                self.q.put(("canceled", None))
                return
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

        # 단일 프로세스(안전) vs 병렬(빠름)
        if not self.parallel or len(to_process) <= 1:
            for idx, path in enumerate(pdfs, start=1):
                if self.cancel.is_set():
                    self.q.put(("canceled", None))
                    return

                self.q.put(("progress", IndexProgress(idx, total, path)))

                if path not in to_process:
                    continue

                try:
                    mtime, size = metas.get(path, (int(os.stat(path).st_mtime), int(os.stat(path).st_size)))
                    doc_id = db.upsert_document(path, mtime, size, status="ok")
                    pages_text = extract_pdf_text_pages(path)
                    db.replace_pages(doc_id, pages_text, build_ngram=self.build_ngram)
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
            # 병렬: 변경 파일만 병렬 추출 후 DB는 메인 스레드에서 반영(경합 방지)
            workers = max(1, min(self.max_workers, os.cpu_count() or 2))
            self.q.put(("parallel", workers))
            futures = {}
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for path in to_process:
                    if self.cancel.is_set():
                        self.q.put(("canceled", None))
                        return
                    futures[ex.submit(extract_pdf_text_pages, path)] = path

                done_count = 0
                for fut in as_completed(futures):
                    if self.cancel.is_set():
                        self.q.put(("canceled", None))
                        return
                    path = futures[fut]
                    done_count += 1
                    self.q.put(("extract_done", (done_count, len(to_process), path)))

                    try:
                        pages_text = fut.result()
                        mtime, size = metas.get(path, (int(os.stat(path).st_mtime), int(os.stat(path).st_size)))
                        doc_id = db.upsert_document(path, mtime, size, status="ok")
                        db.replace_pages(doc_id, pages_text, build_ngram=self.build_ngram)
                        db.upsert_document(path, mtime, size, status="ok")
                    except Exception as e:
                        errors.append(IndexErrorItem(path, str(e)))
                        try:
                            st2 = os.stat(path)
                            db.upsert_document(path, int(st2.st_mtime), int(st2.st_size), status="error")
                        except Exception:
                            pass
                        self.q.put(("error", errors[-1]))

            # 진행률 표시(전체 pdf 기준)
            # (병렬은 추출 완료 이벤트로 별도 표시)

        self.q.put(("errors_done", errors))


# -----------------------------
# Synonyms UI dialog
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
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="필터:").pack(side="left")
        ttk.Entry(top, textvariable=self.filter_var, width=30).pack(side="left", padx=6)
        ttk.Button(top, text="적용", command=self._reload).pack(side="left")
        ttk.Button(top, text="synonyms.json 내보내기", command=self._export).pack(side="right", padx=4)
        ttk.Button(top, text="synonyms.json 가져오기", command=self._import).pack(side="right", padx=4)

        mid = ttk.Frame(self, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(mid, columns=("term", "syn"), show="headings")
        self.tree.heading("term", text="term")
        self.tree.heading("syn", text="synonym")
        self.tree.column("term", width=300, anchor="w")
        self.tree.column("syn", width=540, anchor="w")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        bottom = ttk.LabelFrame(self, text="추가/삭제", padding=10)
        bottom.pack(fill="x", padx=10, pady=(0, 10))

        row1 = ttk.Frame(bottom)
        row1.pack(fill="x")
        ttk.Label(row1, text="term").pack(side="left")
        ttk.Entry(row1, textvariable=self.term_var, width=30).pack(side="left", padx=6)
        ttk.Label(row1, text="synonym").pack(side="left")
        ttk.Entry(row1, textvariable=self.syn_var, width=45).pack(side="left", padx=6)
        ttk.Button(row1, text="추가", command=self._add).pack(side="left", padx=6)

        row2 = ttk.Frame(bottom)
        row2.pack(fill="x", pady=(8, 0))
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
                if flt:
                    if flt not in term.lower() and flt not in s.lower():
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
        self.term_var.set("")
        self.syn_var.set("")
        self._reload()
        self.on_changed_callback()

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if not values:
            return
        term, syn = values[0], values[1]
        db = IndexDB(self.db_path)
        db.delete_synonym(term, syn)
        db.close()
        self._reload()
        self.on_changed_callback()

    def _delete_term(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if not values:
            return
        term = values[0]
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
# Main GUI
# -----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 640)

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

    # ---- settings ----
    def _load_settings(self) -> dict:
        if not os.path.exists(SETTINGS_JSON):
            # 기본값
            s = {
                "viewer_mode": "",  # "", "sumatra", "acrobat", "custom"
                "viewer_path": "",
                "viewer_custom_args": ["{viewer}", "{file}"],
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

    # ---- init ----
    def _ensure_db(self):
        try:
            db = IndexDB(self.db_path)
            db.close()
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
            self.syn_status_var.set(f"동의어: {len(mp)}개 term 로드됨 (synonyms.json)")
        except Exception as e:
            self.syn_graph = {}
            self.syn_status_var.set(f"동의어 로드 실패: {e}")

    # ---- UI ----
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        self.folder_var = tk.StringVar(value="")
        ttk.Label(top, text="대상 폴더:").pack(side="left")
        ttk.Entry(top, textvariable=self.folder_var, width=68).pack(side="left", padx=6)
        ttk.Button(top, text="폴더 선택", command=self.on_pick_folder).pack(side="left", padx=4)

        ttk.Button(top, text="인덱싱 시작", command=self.on_start_index).pack(side="left", padx=4)
        ttk.Button(top, text="취소", command=self.on_cancel_index).pack(side="left", padx=4)
        ttk.Button(top, text="동의어 관리", command=self.on_manage_synonyms).pack(side="left", padx=8)
        ttk.Button(top, text="뷰어 설정", command=self.on_viewer_settings).pack(side="left", padx=4)

        # Progress
        prog = ttk.Frame(self, padding=(10, 0, 10, 10))
        prog.pack(fill="x")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(prog, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="준비됨. 폴더를 선택하고 인덱싱을 시작하세요.")
        ttk.Label(prog, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))
        self.syn_status_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.syn_status_var).pack(anchor="w", pady=(2, 0))

        # Index options
        opt = ttk.LabelFrame(self, text="인덱싱 옵션", padding=10)
        opt.pack(fill="x", padx=10, pady=(0, 10))

        self.build_ngram_var = tk.BooleanVar(value=False)
        self.parallel_var = tk.BooleanVar(value=False)
        self.workers_var = tk.IntVar(value=min(4, os.cpu_count() or 2))

        ttk.Checkbutton(opt, text="한국어 n-gram(2-gram) 보조 인덱스 생성(검색 품질↑, 용량↑)", variable=self.build_ngram_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opt, text="병렬 PDF 추출(빠름, CPU 사용↑)", variable=self.parallel_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="workers:").grid(row=1, column=1, sticky="e", padx=(12, 2))
        ttk.Spinbox(opt, from_=1, to=max(1, os.cpu_count() or 2), textvariable=self.workers_var, width=6).grid(row=1, column=2, sticky="w")

        # Search area
        search_frame = ttk.LabelFrame(self, text="검색", padding=10)
        search_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.query_var = tk.StringVar(value="")
        ttk.Label(search_frame, text='검색어(구문은 "따옴표" 가능):').grid(row=0, column=0, sticky="w")
        ttk.Entry(search_frame, textvariable=self.query_var, width=78).grid(row=0, column=1, sticky="w", padx=6)

        self.expand_var = tk.BooleanVar(value=False)
        self.ngram_search_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(search_frame, text="동의어 확장 검색", variable=self.expand_var).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Checkbutton(search_frame, text="n-gram 보조 검색 사용(인덱스 생성 시)", variable=self.ngram_search_var).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Button(search_frame, text="검색", command=self.on_search).grid(row=0, column=4, sticky="w", padx=6)

        # Results
        mid = ttk.Frame(self, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        cols = ("path", "page", "snippet")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        self.tree.heading("path", text="파일 경로")
        self.tree.heading("page", text="페이지")
        self.tree.heading("snippet", text="스니펫(일부 미리보기)")
        self.tree.column("path", width=560, anchor="w")
        self.tree.column("page", width=70, anchor="center")
        self.tree.column("snippet", width=500, anchor="w")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self.on_open_selected)

        # Errors
        bottom = ttk.LabelFrame(self, text="오류 목록(인덱싱 중)", padding=10)
        bottom.pack(fill="both", padx=10, pady=(0, 10))
        self.err_text = tk.Text(bottom, height=7, wrap="word")
        self.err_text.pack(fill="both", expand=True)

    # -----------------------------
    # UI handlers
    # -----------------------------
    def on_pick_folder(self):
        folder = filedialog.askdirectory(title="PDF 폴더 선택")
        if folder:
            self.folder_var.set(folder)

    def on_manage_synonyms(self):
        SynonymsDialog(self, self.db_path, on_changed_callback=self._load_synonyms)

    def on_viewer_settings(self):
        win = tk.Toplevel(self)
        win.title("PDF 뷰어 설정(페이지 점프)")
        win.geometry("720x340")
        win.minsize(680, 320)

        mode_var = tk.StringVar(value=(self.settings.get("viewer_mode") or ""))
        path_var = tk.StringVar(value=(self.settings.get("viewer_path") or ""))
        custom_args_var = tk.StringVar(value=json.dumps(self.settings.get("viewer_custom_args") or ["{viewer}", "{file}"], ensure_ascii=False))

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="모드:").grid(row=0, column=0, sticky="w")
        mode_cb = ttk.Combobox(frm, textvariable=mode_var, values=["", "sumatra", "acrobat", "custom"], state="readonly", width=14)
        mode_cb.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="(빈값이면 자동: Sumatra 있으면 사용 → 아니면 기본 열기)").grid(row=0, column=2, sticky="w")

        ttk.Label(frm, text="뷰어 exe 경로:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=path_var, width=70).grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(10, 0))
        def pick_exe():
            p = filedialog.askopenfilename(title="뷰어 exe 선택", filetypes=[("EXE", "*.exe"), ("All", "*.*")])
            if p:
                path_var.set(p)
        ttk.Button(frm, text="찾기", command=pick_exe).grid(row=1, column=3, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="custom args(JSON list):").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=custom_args_var, width=70).grid(row=2, column=1, columnspan=3, sticky="w", padx=6, pady=(10, 0))
        ttk.Label(frm, text='토큰: {viewer} {file} {page}  예) ["{viewer}", "-page", "{page}", "{file}"]').grid(row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))

        def save():
            self.settings["viewer_mode"] = mode_var.get().strip()
            self.settings["viewer_path"] = path_var.get().strip()
            try:
                args = json.loads(custom_args_var.get().strip())
                if not isinstance(args, list):
                    raise ValueError
                self.settings["viewer_custom_args"] = args
            except Exception:
                messagebox.showwarning("형식 오류", "custom args는 JSON 배열(list)이어야 합니다.")
                return
            self._save_settings()
            messagebox.showinfo("저장", "설정을 저장했습니다.")
            win.destroy()

        ttk.Button(frm, text="저장", command=save).grid(row=4, column=3, sticky="e", pady=(18, 0))

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
        )
        self.indexer.start()

    def on_cancel_index(self):
        if self.indexer and self.indexer.is_alive():
            self.cancel_event.set()
            self.status_var.set("취소 요청됨… (안전하게 중단 중)")
        else:
            self.status_var.set("실행 중인 인덱싱이 없습니다.")

    def on_search(self):
        user_q = self.query_var.get().strip()
        if not user_q:
            messagebox.showinfo("검색어 없음", "검색어를 입력하세요.")
            return

        try:
            db = IndexDB(self.db_path)

            # 1) 기본 텍스트 FTS
            fts_q = make_fts_query(user_q, self.expand_var.get(), self.syn_graph)
            rows = db.search_text(fts_q, limit=250) if fts_q else []

            # 2) n-gram 보조 검색(옵션)
            if self.ngram_search_var.get():
                ng_q = make_ngram_query(user_q, self.expand_var.get(), self.syn_graph)
                if ng_q:
                    ng_rows = db.search_ngram(ng_q, limit=250)
                else:
                    ng_rows = []
            else:
                ng_q, ng_rows = "", []

            db.close()

            # 결과 머지: (path,page) 기준으로 합치고 점수는 대충 랭크 조정
            merged: Dict[Tuple[str, int], Tuple[str, float]] = {}
            # 기본 검색을 우선
            for r in rows:
                key = (r["path"], int(r["page_no"]))
                merged[key] = (r["snippet"] or "", float(r["score"]))

            for r in ng_rows:
                key = (r["path"], int(r["page_no"]))
                # 이미 있으면 유지, 없으면 추가
                if key not in merged:
                    # ngram score는 bm25지만 테이블이 다르니 약간 페널티(너무 위로 올라오는 것 방지)
                    merged[key] = (r["snippet"] or "", float(r["score"]) + 2.0)

            # score 오름차순(FTS bm25는 낮을수록 더 관련)
            # nsmallest: 전체 정렬 없이 top-500만 추출 (O(n log k) vs O(n log n))
            top_items = nsmallest(500, merged.items(), key=lambda kv: kv[1][1])

            # Tree update
            for item in self.tree.get_children():
                self.tree.delete(item)

            for (path, page_no), (snippet, _score) in top_items:
                self.tree.insert("", "end", values=(path, page_no, snippet))

            msg = f"검색 완료: {len(merged)}건"
            if fts_q:
                msg += f" | textFTS: {fts_q}"
            if self.ngram_search_var.get() and ng_q:
                msg += f" | ngramFTS: {ng_q}"
            self.status_var.set(msg)

        except Exception as e:
            messagebox.showerror("검색 실패", str(e))

    def on_open_selected(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if not values:
            return
        path = values[0]
        page = int(values[1]) if len(values) > 1 else 1
        try:
            open_pdf_at_page(path, page, self.settings)
        except Exception as e:
            messagebox.showerror("열기 실패", str(e))

    # -----------------------------
    # Background event polling
    # -----------------------------
    def _poll_events(self):
        try:
            while True:
                typ, payload = self.event_q.get_nowait()

                if typ == "scan":
                    total = int(payload)
                    self._set_progress(0, max(total, 1))
                    self.status_var.set(f"PDF 스캔 완료. 총 {total}개 파일. 인덱싱 시작…")

                elif typ == "plan":
                    changed, total = payload
                    self.status_var.set(f"변경/신규 {changed}개 / 총 {total}개. 처리 중…")

                elif typ == "parallel":
                    w = int(payload)
                    self.status_var.set(f"병렬 추출 모드: workers={w} (DB 반영은 단일)")

                elif typ == "progress":
                    p: IndexProgress = payload
                    self._set_progress(p.current, max(p.total, 1))
                    self.status_var.set(f"[{p.current}/{p.total}] 인덱싱(단일): {p.path}")

                elif typ == "extract_done":
                    done, total_changed, path = payload
                    # 병렬 모드 진행 표시(변경 대상 기준)
                    pct = (done / max(1, total_changed)) * 100.0
                    self.progress_var.set(max(0.0, min(100.0, pct)))
                    self.status_var.set(f"[{done}/{total_changed}] 추출 완료 → DB 반영: {path}")

                elif typ == "deleted":
                    self.status_var.set(f"삭제/이동 반영: {payload}")

                elif typ == "error":
                    err: IndexErrorItem = payload
                    self.err_text.insert("end", f"- {err.path}\n  {err.error}\n")
                    self.err_text.see("end")

                elif typ == "errors_done":
                    errors: List[IndexErrorItem] = payload
                    if errors:
                        self.status_var.set(f"인덱싱 완료(오류 {len(errors)}건). 아래 로그 확인.")
                    else:
                        self.status_var.set("인덱싱 완료(오류 없음).")

                elif typ == "canceled":
                    self.status_var.set("인덱싱이 취소되었습니다.")
                    self._set_progress(0, 1)

                elif typ == "done":
                    self.status_var.set("인덱싱 완료. 이제 검색하세요.")
                    self.progress_var.set(100.0)

                elif typ == "fatal":
                    messagebox.showerror("치명적 오류", str(payload))
                    self.status_var.set("오류로 중단됨.")
        except queue.Empty:
            pass
        finally:
            self.after(120, self._poll_events)

    def _set_progress(self, cur: int, total: int):
        if total <= 0:
            self.progress_var.set(0)
            return
        pct = (cur / total) * 100.0
        self.progress_var.set(max(0.0, min(100.0, pct)))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()