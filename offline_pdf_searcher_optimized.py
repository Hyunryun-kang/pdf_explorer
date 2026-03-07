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
import tkinter.font as tkfont

try:
    import fitz
except Exception:
    fitz = None

from concurrent.futures import ProcessPoolExecutor, as_completed

APP_NAME  = "PDF Search"
DB_FILENAME   = "offline_pdf_index.db"
SYNONYMS_JSON = "synonyms.json"
SETTINGS_JSON = "settings.json"

# ══════════════════════════════════════════════
#  성경 약어 → 전체 이름  (동의어 사전 초기 데이터)
#  term=약어, synonym=전체이름  으로 DB에 등록됩니다.
# ══════════════════════════════════════════════
BIBLE_BOOK_MAP: Dict[str, str] = {
    # 구약
    "창": "창세기",       "출": "출애굽기",     "레": "레위기",
    "민": "민수기",       "신": "신명기",       "수": "여호수아",
    "삿": "사사기",       "룻": "룻기",         "삼상": "사무엘상",
    "삼하": "사무엘하",   "왕상": "열왕기상",   "왕하": "열왕기하",
    "대상": "역대상",     "대하": "역대하",     "스": "에스라",
    "느": "느헤미야",     "에": "에스더",       "욥": "욥기",
    "시": "시편",         "잠": "잠언",         "전": "전도서",
    "아": "아가",         "사": "이사야",       "렘": "예레미야",
    "애": "예레미야애가", "겔": "에스겔",       "단": "다니엘",
    "호": "호세아",       "욜": "요엘",         "암": "아모스",
    "옵": "오바댜",       "욘": "요나",         "미": "미가",
    "나": "나훔",         "합": "하박국",       "습": "스바냐",
    "학": "학개",         "슥": "스가랴",       "말": "말라기",
    # 신약
    "마": "마태복음",     "막": "마가복음",     "눅": "누가복음",
    "요": "요한복음",     "행": "사도행전",     "롬": "로마서",
    "고전": "고린도전서", "고후": "고린도후서", "갈": "갈라디아서",
    "엡": "에베소서",     "빌": "빌립보서",     "골": "골로새서",
    "살전": "데살로니가전서", "살후": "데살로니가후서",
    "딤전": "디모데전서", "딤후": "디모데후서", "딛": "디도서",
    "몬": "빌레몬서",     "히": "히브리서",     "약": "야고보서",
    "벧전": "베드로전서", "벧후": "베드로후서", "요일": "요한일서",
    "요이": "요한이서",   "요삼": "요한삼서",   "유": "유다서",
    "계": "요한계시록",
}

# ══════════════════════════════════════════════
#  Design tokens  (Notion × Claude dark theme)
# ══════════════════════════════════════════════
C = {
    "bg":           "#191919",
    "sidebar":      "#1f1f1f",
    "panel":        "#232323",
    "panel2":       "#2a2a2a",
    "input_bg":     "#272727",
    "hover":        "#2e2e2e",
    "active":       "#353535",
    "border":       "#333333",
    "border_soft":  "#2b2b2b",

    "text":         "#e3e3e3",
    "text2":        "#999999",
    "text3":        "#555555",
    "text_inv":     "#ffffff",

    "accent":       "#d97757",
    "accent2":      "#c96644",
    "accent_dim":   "#2e1c12",
    "accent_text":  "#f5c4b0",

    "green":        "#4caf82",
    "green_bg":     "#182a20",
    "red":          "#e05c5c",
    "red_bg":       "#2a1818",
    "yellow":       "#d4a843",
    "yellow_bg":    "#2a2110",
}

SANS = ("Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", "Segoe UI", "sans-serif")
MONO = ("JetBrains Mono", "Fira Code", "Consolas", "Courier New", "monospace")

def pfont(families, size=12, weight="normal"):
    avail = tkfont.families()
    for f in families:
        if f in avail:
            return (f, size, weight)
    return (families[-1], size, weight)

# ══════════════════════════════════════════════
#  Utility / Core logic  (unchanged from original)
# ══════════════════════════════════════════════
def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_pdf(path):
    return path.lower().endswith(".pdf")

def safe_fts_quote(term):
    return term.replace('"', '""').strip()

def normalize_ws(s):
    return " ".join((s or "").replace("\x00", " ").split())

def char_2grams(text):
    t = normalize_ws(text)[:120_000]
    if not t: return ""
    t2 = t.replace(" ", "")
    if len(t2) < 2: return t2
    gram_count = len(t2) - 1
    step = max(1, gram_count // 250_000) if gram_count > 250_000 else 1
    return " ".join(t2[i:i+2] for i in range(0, gram_count, step))

def open_file_default(path):
    try: os.startfile(path)
    except Exception as e: raise RuntimeError(f"파일 열기 실패: {e}")

def find_sumatra_exe():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "SumatraPDF", "SumatraPDF.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "SumatraPDF", "SumatraPDF.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c): return c
    return shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")

def open_pdf_at_page(path, page_no_1base, settings):
    page_no_1base = max(1, int(page_no_1base))
    mode  = (settings.get("viewer_mode") or "").lower().strip()
    vpath = (settings.get("viewer_path") or "").strip()
    def run(cmd): subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mode and vpath and os.path.exists(vpath):
        if mode == "sumatra": run([vpath, "-reuse-instance", "-page", str(page_no_1base), path]); return
        if mode == "acrobat": run([vpath, path, "/A", f"page={page_no_1base}"]); return
        if mode == "custom":
            tmpl = settings.get("viewer_custom_args") or []
            if isinstance(tmpl, list) and tmpl:
                cmd = [t.replace("{viewer}", vpath).replace("{file}", path).replace("{page}", str(page_no_1base))
                       for t in tmpl if isinstance(t, str)]
                if cmd: run(cmd); return
    sumatra = find_sumatra_exe()
    if sumatra: run([sumatra, "-reuse-instance", "-page", str(page_no_1base), path]); return
    open_file_default(path)

# ── DB ────────────────────────────────────────
SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    file_mtime INTEGER NOT NULL, file_size INTEGER NOT NULL,
    last_indexed_at TEXT, status TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL, page_no INTEGER NOT NULL, text TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
    USING fts5(text, content='pages', content_rowid='page_id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid,text) VALUES (new.page_id,new.text); END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts,rowid,text) VALUES ('delete',old.page_id,old.text); END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts,rowid,text) VALUES ('delete',old.page_id,old.text);
    INSERT INTO pages_fts(rowid,text) VALUES (new.page_id,new.text); END;
CREATE TABLE IF NOT EXISTS pages_ng (
    page_id INTEGER PRIMARY KEY, grams TEXT NOT NULL,
    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_ng_fts
    USING fts5(grams, content='pages_ng', content_rowid='page_id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS pagesng_ai AFTER INSERT ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(rowid,grams) VALUES (new.page_id,new.grams); END;
CREATE TRIGGER IF NOT EXISTS pagesng_ad AFTER DELETE ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(pages_ng_fts,rowid,grams) VALUES ('delete',old.page_id,old.grams); END;
CREATE TRIGGER IF NOT EXISTS pagesng_au AFTER UPDATE ON pages_ng BEGIN
    INSERT INTO pages_ng_fts(pages_ng_fts,rowid,grams) VALUES ('delete',old.page_id,old.grams);
    INSERT INTO pages_ng_fts(rowid,grams) VALUES (new.page_id,new.grams); END;
CREATE TABLE IF NOT EXISTS synonyms (
    term TEXT NOT NULL, synonym TEXT NOT NULL, PRIMARY KEY (term, synonym));
CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path);
CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(doc_id);
"""

class IndexDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        for p in ["PRAGMA foreign_keys=ON;","PRAGMA journal_mode=WAL;",
                  "PRAGMA synchronous=NORMAL;","PRAGMA temp_store=MEMORY;",
                  "PRAGMA cache_size=-200000;","PRAGMA mmap_size=67108864;"]:
            self._conn.execute(p)
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._conn.execute("SELECT 1 FROM pages_fts LIMIT 1;")
        self._conn.execute("SELECT 1 FROM pages_ng_fts LIMIT 1;")

    def close(self):
        try: self._conn.close()
        except: pass

    def upsert_document(self, path, mtime, size, status="ok"):
        cur = self._conn.cursor()
        cur.execute("""INSERT INTO documents(path,file_mtime,file_size,last_indexed_at,status)
            VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            file_mtime=excluded.file_mtime,file_size=excluded.file_size,
            last_indexed_at=excluded.last_indexed_at,status=excluded.status""",
            (path,mtime,size,now_iso(),status))
        self._conn.commit()
        cur.execute("SELECT doc_id FROM documents WHERE path=?",(path,))
        return int(cur.fetchone()["doc_id"])

    def get_document_meta(self, path):
        cur = self._conn.cursor()
        cur.execute("SELECT doc_id,file_mtime,file_size FROM documents WHERE path=?",(path,))
        r = cur.fetchone()
        return (int(r["doc_id"]),int(r["file_mtime"]),int(r["file_size"])) if r else None

    def list_document_paths(self):
        cur = self._conn.cursor()
        cur.execute("SELECT path FROM documents")
        return {r["path"] for r in cur.fetchall()}

    def delete_document_by_path(self, path):
        self._conn.execute("DELETE FROM documents WHERE path=?",(path,))
        self._conn.commit()

    def replace_pages(self, doc_id, pages_text, build_ngram):
        cur = self._conn.cursor()
        cur.execute("DELETE FROM pages WHERE doc_id=?",(doc_id,))
        cur.executemany("INSERT INTO pages(doc_id,page_no,text) VALUES(?,?,?)",
                        [(doc_id,pno,txt) for pno,txt in pages_text])
        if build_ngram:
            pno_to_text = {pno:txt for pno,txt in pages_text}
            cur.execute("SELECT page_id,page_no FROM pages WHERE doc_id=? ORDER BY page_no",(doc_id,))
            cur.executemany("INSERT OR REPLACE INTO pages_ng(page_id,grams) VALUES(?,?)",
                [(int(r["page_id"]),char_2grams(pno_to_text.get(r["page_no"],""))) for r in cur.fetchall()])
        else:
            cur.execute("DELETE FROM pages_ng WHERE page_id IN "
                        "(SELECT page_id FROM pages WHERE doc_id=?)",(doc_id,))
        self._conn.commit()

    def load_synonyms_map(self):
        cur = self._conn.cursor()
        cur.execute("SELECT term,synonym FROM synonyms")
        mp = {}
        for r in cur.fetchall(): mp.setdefault(r["term"],set()).add(r["synonym"])
        return mp

    def add_synonym(self, term, synonym):
        term,synonym = term.strip(),synonym.strip()
        if term and synonym:
            self._conn.execute("INSERT OR IGNORE INTO synonyms(term,synonym) VALUES(?,?)",(term,synonym))
            self._conn.commit()

    def delete_synonym(self, term, synonym):
        self._conn.execute("DELETE FROM synonyms WHERE term=? AND synonym=?",(term,synonym))
        self._conn.commit()

    def delete_term(self, term):
        self._conn.execute("DELETE FROM synonyms WHERE term=?",(term,))
        self._conn.commit()

    def export_synonyms_to_json(self, json_path):
        mp = self.load_synonyms_map()
        data = {k:sorted(list(vs)) for k,vs in sorted(mp.items(),key=lambda x:x[0].lower())}
        with open(json_path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)

    def import_synonyms_from_json(self, json_path):
        if not os.path.exists(json_path):
            with open(json_path,"w",encoding="utf-8") as f:
                json.dump({"예시":["샘플","견본"],"PDF":["피디에프"]},f,ensure_ascii=False,indent=2)
            return
        with open(json_path,"r",encoding="utf-8") as f: data = json.load(f)
        if not isinstance(data,dict): raise ValueError("synonyms.json은 {term:[...]} 형태여야 합니다.")
        rows = [(t.strip(),s.strip()) for t,ss in data.items()
                if isinstance(t,str) and isinstance(ss,list)
                for s in ss if isinstance(s,str) and s.strip()]
        self._conn.executemany("INSERT OR IGNORE INTO synonyms(term,synonym) VALUES(?,?)",rows)
        self._conn.commit()

    def seed_bible_synonyms(self) -> int:
        """BIBLE_BOOK_MAP의 약어↔전체이름을 양방향으로 synonyms 테이블에 등록.
        이미 존재하는 항목은 INSERT OR IGNORE로 건너뜁니다.
        반환값: 새로 삽입된 행 수."""
        rows = []
        for abbr, full in BIBLE_BOOK_MAP.items():
            rows.append((abbr, full))   # 약어 → 전체이름
            rows.append((full, abbr))   # 전체이름 → 약어 (역방향 확장)
        cur = self._conn.cursor()
        cur.executemany("INSERT OR IGNORE INTO synonyms(term,synonym) VALUES(?,?)", rows)
        inserted = cur.rowcount
        self._conn.commit()
        return inserted

    def search_text(self, fts_query, limit=250):
        cur = self._conn.cursor()
        cur.execute("""SELECT d.path,p.page_no,
            snippet(pages_fts,0,'[',']',' … ',12) AS snippet, bm25(pages_fts) AS score
            FROM pages_fts JOIN pages p ON p.page_id=pages_fts.rowid
            JOIN documents d ON d.doc_id=p.doc_id
            WHERE pages_fts MATCH ? ORDER BY score LIMIT ?""",(fts_query,limit))
        return cur.fetchall()

    def search_ngram(self, grams_query, limit=250):
        cur = self._conn.cursor()
        cur.execute("""SELECT d.path,p.page_no,
            snippet(pages_ng_fts,0,'[',']',' … ',12) AS snippet, bm25(pages_ng_fts) AS score
            FROM pages_ng_fts JOIN pages p ON p.page_id=pages_ng_fts.rowid
            JOIN documents d ON d.doc_id=p.doc_id
            WHERE pages_ng_fts MATCH ? ORDER BY score LIMIT ?""",(grams_query,limit))
        return cur.fetchall()

# ── Query helpers ─────────────────────────────
def build_graph(mp):
    g = {}
    for k,vs in mp.items(): g.setdefault(k,set()).update(vs)
    return g

def expand_terms(base_terms, graph, max_depth=2):
    out = {t for t in base_terms if t.strip()}
    visited = set(out)
    q = deque((t,0) for t in out)
    while q:
        term,depth = q.popleft()
        if depth >= max_depth: continue
        for nxt in graph.get(term,set()):
            nxt = nxt.strip()
            if not nxt or nxt in visited: continue
            visited.add(nxt); out.add(nxt); q.append((nxt,depth+1))
    return out

def parse_query(user_query):
    q = user_query.strip()
    if not q: return [],[]
    phrases,tokens = [],[]
    i = 0
    while i < len(q):
        if q[i] == '"':
            j = i+1
            while j < len(q) and q[j] != '"': j += 1
            if j < len(q):
                ph = q[i+1:j].strip()
                if ph: phrases.append(ph)
                i = j+1
            else:
                rest = q[i+1:].strip()
                if rest: tokens.extend(rest.split())
                break
        else:
            j = i
            while j < len(q) and q[j] != '"': j += 1
            chunk = q[i:j].strip()
            if chunk: tokens.extend(chunk.split())
            i = j
    return tokens,phrases

def make_fts_query(user_query, expanded, syn_graph):
    tokens,phrases = parse_query(user_query)
    parts = [f'"{safe_fts_quote(ph)}"' for ph in phrases]
    final_terms = expand_terms(tokens,syn_graph) if expanded and tokens else set(tokens)
    parts += [f'"{safe_fts_quote(t)}"' for t in sorted(final_terms) if safe_fts_quote(t)]
    return " OR ".join(parts)

def make_ngram_query(user_query, expanded, syn_graph):
    tokens,phrases = parse_query(user_query)
    all_terms = tokens+phrases
    if expanded and tokens: all_terms = list(expand_terms(tokens,syn_graph))+phrases
    clauses = []
    for term in all_terms:
        term = term.strip()
        if not term: continue
        grams = char_2grams(term)
        if not grams: continue
        g_tokens = grams.split()
        ands = " AND ".join([f'"{safe_fts_quote(gt)}"' for gt in g_tokens[:80]])
        if ands: clauses.append(f"({ands})")
    return " OR ".join(clauses)

# ── Indexer ───────────────────────────────────
@dataclass
class IndexProgress:
    current: int; total: int; path: str

@dataclass
class IndexErrorItem:
    path: str; error: str

def extract_pdf_text_pages(path):
    if fitz is None: raise RuntimeError("PyMuPDF 미설치")
    pages_text = []
    with fitz.open(path) as doc:
        for pno in range(doc.page_count):
            text = doc.load_page(pno).get_text("text") or ""
            pages_text.append((pno+1, normalize_ws(text)))
    return pages_text

class IndexerThread(threading.Thread):
    def __init__(self, db_path, root_folder, event_queue, cancel_event,
                 build_ngram, parallel, max_workers):
        super().__init__(daemon=True)
        self.db_path = db_path; self.root_folder = root_folder
        self.q = event_queue; self.cancel = cancel_event
        self.build_ngram = build_ngram; self.parallel = parallel; self.max_workers = max_workers

    def run(self):
        if fitz is None:
            self.q.put(("fatal","PyMuPDF가 필요합니다: pip install pymupdf")); return
        db = None
        try:
            db = IndexDB(self.db_path); self._index(db); self.q.put(("done",None))
        except Exception as e:
            self.q.put(("fatal",f"{e}\n\n{traceback.format_exc()}"))
        finally:
            if db: db.close()

    def _collect_pdfs(self):
        out = []
        for base,_,files in os.walk(self.root_folder):
            if self.cancel.is_set(): break
            for fn in files:
                if fn.lower().endswith(".pdf"): out.append(os.path.join(base,fn))
        return out

    def _index(self, db):
        pdfs = self._collect_pdfs(); total = len(pdfs)
        self.q.put(("scan",total))
        existing = db.list_document_paths()
        for path in sorted(existing-set(pdfs)):
            if self.cancel.is_set(): self.q.put(("canceled",None)); return
            db.delete_document_by_path(path); self.q.put(("deleted",path))

        to_process,metas = [],{}
        for path in pdfs:
            if self.cancel.is_set(): self.q.put(("canceled",None)); return
            try:
                st = os.stat(path); mtime,size = int(st.st_mtime),int(st.st_size)
                metas[path] = (mtime,size)
                meta = db.get_document_meta(path)
                if not (meta and meta[1]==mtime and meta[2]==size): to_process.append(path)
            except: to_process.append(path)
        self.q.put(("plan",(len(to_process),total)))

        errors = []
        if not self.parallel or len(to_process) <= 1:
            for idx,path in enumerate(pdfs,1):
                if self.cancel.is_set(): self.q.put(("canceled",None)); return
                self.q.put(("progress",IndexProgress(idx,total,path)))
                if path not in to_process: continue
                try:
                    mtime,size = metas.get(path,(int(os.stat(path).st_mtime),int(os.stat(path).st_size)))
                    doc_id = db.upsert_document(path,mtime,size,status="ok")
                    db.replace_pages(doc_id,extract_pdf_text_pages(path),build_ngram=self.build_ngram)
                    db.upsert_document(path,mtime,size,status="ok")
                except Exception as e:
                    errors.append(IndexErrorItem(path,str(e)))
                    try: st2=os.stat(path); db.upsert_document(path,int(st2.st_mtime),int(st2.st_size),status="error")
                    except: pass
                    self.q.put(("error",errors[-1]))
        else:
            workers = max(1,min(self.max_workers,os.cpu_count() or 2))
            self.q.put(("parallel",workers))
            futures = {}
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for path in to_process:
                    if self.cancel.is_set(): self.q.put(("canceled",None)); return
                    futures[ex.submit(extract_pdf_text_pages,path)] = path
                done_count = 0
                for fut in as_completed(futures):
                    if self.cancel.is_set(): self.q.put(("canceled",None)); return
                    path = futures[fut]; done_count += 1
                    self.q.put(("extract_done",(done_count,len(to_process),path)))
                    try:
                        pages_text = fut.result()
                        mtime,size = metas.get(path,(int(os.stat(path).st_mtime),int(os.stat(path).st_size)))
                        doc_id = db.upsert_document(path,mtime,size,status="ok")
                        db.replace_pages(doc_id,pages_text,build_ngram=self.build_ngram)
                        db.upsert_document(path,mtime,size,status="ok")
                    except Exception as e:
                        errors.append(IndexErrorItem(path,str(e)))
                        try: st2=os.stat(path); db.upsert_document(path,int(st2.st_mtime),int(st2.st_size),status="error")
                        except: pass
                        self.q.put(("error",errors[-1]))
        self.q.put(("errors_done",errors))

# ══════════════════════════════════════════════
#  Custom UI widgets
# ══════════════════════════════════════════════

class FlatButton(tk.Canvas):
    """Pill-shaped flat button with hover effect."""
    def __init__(self, parent, text, command=None,
                 bg=C["panel2"], fg=C["text"], hbg=C["hover"],
                 accent=False, small=False, **kw):
        px = 10 if small else 14
        py = 4  if small else 7
        fs = 10 if small else 12
        self._text = text
        self._bg   = C["accent"] if accent else bg
        self._fg   = C["text_inv"] if accent else fg
        self._hbg  = C["accent2"] if accent else hbg
        self._cmd  = command
        fnt = pfont(SANS, fs, "bold" if accent else "normal")

        tmp = tk.Label(parent, text=text, font=fnt)
        tw, th = tmp.winfo_reqwidth(), tmp.winfo_reqheight(); tmp.destroy()
        w, h = tw + px*2, th + py*2
        super().__init__(parent, width=w, height=h,
                         bg=C["bg"], highlightthickness=0, **kw)
        self._w, self._h, self._fnt = w, h, fnt
        self._r = h // 2
        self._draw(self._bg)
        self.bind("<Enter>",          lambda e: self._draw(self._hbg))
        self.bind("<Leave>",          lambda e: self._draw(self._bg))
        self.bind("<Button-1>",       lambda e: self._draw(C["active"]))
        self.bind("<ButtonRelease-1>", lambda e: (self._draw(self._bg), self._cmd() if self._cmd else None))

    def _draw(self, color):
        self.delete("all")
        r, w, h = self._r, self._w, self._h
        self.create_arc(0,0,r*2,h,   start=90,  extent=180, fill=color, outline="")
        self.create_arc(w-r*2,0,w,h, start=270, extent=180, fill=color, outline="")
        self.create_rectangle(r,0,w-r,h, fill=color, outline="")
        self.create_text(w//2, h//2, text=self._text, fill=self._fg, font=self._fnt)


class SearchBar(tk.Frame):
    """Rounded search bar with placeholder."""
    def __init__(self, parent, textvariable=None, placeholder="검색어 입력…", **kw):
        super().__init__(parent, bg=C["input_bg"],
                         highlightbackground=C["border"], highlightthickness=1, **kw)
        self._ph  = placeholder
        self._var = textvariable or tk.StringVar()
        fnt = pfont(SANS, 13)
        ico = tk.Label(self, text="⌕", bg=C["input_bg"], fg=C["text3"],
                       font=pfont(SANS, 15)); ico.pack(side="left", padx=(10,4))
        self.entry = tk.Entry(self, textvariable=self._var, bd=0,
                              bg=C["input_bg"], fg=C["text"],
                              insertbackground=C["accent"],
                              selectbackground=C["accent_dim"],
                              selectforeground=C["accent_text"],
                              font=fnt, relief="flat")
        self.entry.pack(side="left", fill="both", expand=True, padx=(0,12), pady=7)
        self.entry.bind("<FocusIn>",  self._on_focus)
        self.entry.bind("<FocusOut>", self._on_blur)
        ico.bind("<Button-1>", lambda e: self.entry.focus_set())
        if not self._var.get(): self._show_ph()

    def _show_ph(self):
        self.entry.config(fg=C["text3"])
        self.entry.delete(0,"end"); self.entry.insert(0, self._ph)

    def _on_focus(self, e):
        self.configure(highlightbackground=C["accent"])
        if self.entry.get() == self._ph:
            self.entry.delete(0,"end"); self.entry.config(fg=C["text"])

    def _on_blur(self, e):
        self.configure(highlightbackground=C["border"])
        if not self.entry.get(): self._show_ph()

    def get(self):
        v = self._var.get()
        return "" if v == self._ph else v


class Toggle(tk.Frame):
    """iOS-style toggle switch."""
    def __init__(self, parent, text, variable, **kw):
        super().__init__(parent, bg=C["panel"], **kw)
        self._var = variable
        self._cv  = tk.Canvas(self, width=36, height=20, bg=C["panel"], highlightthickness=0)
        self._cv.pack(side="left", padx=(0,7))
        tk.Label(self, text=text, bg=C["panel"], fg=C["text2"],
                 font=pfont(SANS,11)).pack(side="left")
        self._draw()
        self._cv.bind("<Button-1>", self._toggle)
        self.bind("<Button-1>",     self._toggle)

    def _toggle(self, e=None):
        self._var.set(not self._var.get()); self._draw()

    def _draw(self):
        self._cv.delete("all")
        on = self._var.get()
        bg = C["accent"] if on else C["border"]
        self._cv.create_arc(1,1,19,19,   start=90,  extent=180, fill=bg, outline="")
        self._cv.create_arc(17,1,35,19,  start=270, extent=180, fill=bg, outline="")
        self._cv.create_rectangle(10,1,26,19, fill=bg, outline="")
        x = 26 if on else 10
        self._cv.create_oval(x-7,3,x+7,17, fill="white", outline="")


class ProgressStrip(tk.Canvas):
    """Thin 4px progress bar."""
    def __init__(self, parent, **kw):
        super().__init__(parent, height=4, bg=C["border_soft"], highlightthickness=0, **kw)
        self._pct = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, pct):
        self._pct = max(0.0, min(100.0, pct)); self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 1
        h = self.winfo_height() or 4
        self.create_rectangle(0,0,w,h, fill=C["border_soft"], outline="")
        filled = int(w * self._pct / 100)
        if filled > 0:
            self.create_rectangle(0,0,filled,h, fill=C["accent"], outline="")


class StatusDot(tk.Canvas):
    """Small colored dot + text status widget."""
    _COLORS = {
        "idle":    C["text3"],
        "running": C["yellow"],
        "ok":      C["green"],
        "error":   C["red"],
    }
    def __init__(self, parent, **kw):
        super().__init__(parent, width=10, height=10,
                         bg=C["panel"], highlightthickness=0, **kw)
        self._status = "idle"
        self._draw()

    def set_status(self, status):
        self._status = status; self._draw()

    def _draw(self):
        self.delete("all")
        color = self._COLORS.get(self._status, C["text3"])
        self.create_oval(1,1,9,9, fill=color, outline="")


# ══════════════════════════════════════════════
#  Synonyms dialog  (modern style)
# ══════════════════════════════════════════════
class SynonymsDialog(tk.Toplevel):
    def __init__(self, parent, db_path, on_changed_callback):
        super().__init__(parent)
        self.title("동의어 관리")
        self.geometry("820x540")
        self.configure(bg=C["bg"])
        self.db_path = db_path
        self.on_changed_callback = on_changed_callback
        self.term_var   = tk.StringVar()
        self.syn_var    = tk.StringVar()
        self.filter_var = tk.StringVar()
        self._build(); self._reload()

    def _build(self):
        # ── header ──
        hdr = tk.Frame(self, bg=C["sidebar"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="동의어 사전", bg=C["sidebar"], fg=C["text"],
                 font=pfont(SANS,14,"bold")).pack(side="left")
        FlatButton(hdr, "JSON 내보내기", self._export, small=True).pack(side="right", padx=(4,0))
        FlatButton(hdr, "JSON 가져오기", self._import, small=True).pack(side="right", padx=4)

        # ── filter bar ──
        fb = tk.Frame(self, bg=C["bg"], padx=16, pady=10)
        fb.pack(fill="x")
        self._fbar = SearchBar(fb, textvariable=self.filter_var, placeholder="필터…")
        self._fbar.pack(side="left", fill="x", expand=True)
        FlatButton(fb, "적용", self._reload, small=True).pack(side="left", padx=8)

        # ── table ──
        tbl = tk.Frame(self, bg=C["bg"], padx=16)
        tbl.pack(fill="both", expand=True)
        style = ttk.Style()
        style.configure("Syn.Treeview",
                        background=C["panel"], fieldbackground=C["panel"],
                        foreground=C["text"], rowheight=30, borderwidth=0,
                        font=pfont(SANS,11))
        style.configure("Syn.Treeview.Heading",
                        background=C["panel2"], foreground=C["text2"],
                        relief="flat", font=pfont(SANS,10), borderwidth=0)
        style.map("Syn.Treeview",
                  background=[("selected",C["accent_dim"])],
                  foreground=[("selected",C["accent_text"])])
        self.tree = ttk.Treeview(tbl, columns=("term","syn"), show="headings",
                                  style="Syn.Treeview")
        self.tree.heading("term", text="Term")
        self.tree.heading("syn",  text="Synonym")
        self.tree.column("term", width=260, anchor="w")
        self.tree.column("syn",  width=480, anchor="w")
        vsb = ttk.Scrollbar(tbl, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ── add / delete ──
        bot = tk.Frame(self, bg=C["sidebar"], padx=16, pady=12)
        bot.pack(fill="x")
        r1 = tk.Frame(bot, bg=C["sidebar"]); r1.pack(fill="x", pady=(0,8))
        tk.Label(r1, text="Term", bg=C["sidebar"], fg=C["text2"],
                 font=pfont(SANS,10), width=6, anchor="w").pack(side="left")
        tk.Entry(r1, textvariable=self.term_var, width=20, bd=0, relief="flat",
                 bg=C["input_bg"], fg=C["text"], font=pfont(SANS,11),
                 insertbackground=C["accent"]).pack(side="left", ipady=5, ipadx=6, padx=(4,14))
        tk.Label(r1, text="Synonym", bg=C["sidebar"], fg=C["text2"],
                 font=pfont(SANS,10)).pack(side="left")
        tk.Entry(r1, textvariable=self.syn_var, width=32, bd=0, relief="flat",
                 bg=C["input_bg"], fg=C["text"], font=pfont(SANS,11),
                 insertbackground=C["accent"]).pack(side="left", ipady=5, ipadx=6, padx=(4,12))
        FlatButton(r1, "추가", self._add, accent=True, small=True).pack(side="left")

        r2 = tk.Frame(bot, bg=C["sidebar"]); r2.pack(fill="x")
        FlatButton(r2, "선택 삭제",       self._delete_selected, small=True).pack(side="left", padx=(0,8))
        FlatButton(r2, "Term 전체 삭제",  self._delete_term,     small=True).pack(side="left")
        self.status_lbl = tk.Label(r2, text="", bg=C["sidebar"],
                                    fg=C["text3"], font=pfont(SANS,10))
        self.status_lbl.pack(side="right")

    def _reload(self):
        for it in self.tree.get_children(): self.tree.delete(it)
        flt = self.filter_var.get().strip().lower()
        if flt == "필터…": flt = ""
        db = IndexDB(self.db_path); mp = db.load_synonyms_map(); db.close()
        rows = [(t,s) for t,ss in mp.items() for s in ss
                if not flt or flt in t.lower() or flt in s.lower()]
        rows.sort(key=lambda x: (x[0].lower(), x[1].lower()))
        for t,s in rows: self.tree.insert("","end",values=(t,s))
        self.status_lbl.config(text=f"{len(rows)}개 항목")

    def _add(self):
        t,s = self.term_var.get().strip(), self.syn_var.get().strip()
        if not t or not s: messagebox.showwarning("값 필요","Term과 Synonym을 입력하세요."); return
        db = IndexDB(self.db_path); db.add_synonym(t,s); db.close()
        self.term_var.set(""); self.syn_var.set("")
        self._reload(); self.on_changed_callback()

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel: return
        t,s = self.tree.item(sel[0],"values")
        db = IndexDB(self.db_path); db.delete_synonym(t,s); db.close()
        self._reload(); self.on_changed_callback()

    def _delete_term(self):
        sel = self.tree.selection()
        if not sel: return
        t,_ = self.tree.item(sel[0],"values")
        if messagebox.askyesno("확인",f"'{t}'의 모든 synonym을 삭제할까요?"):
            db = IndexDB(self.db_path); db.delete_term(t); db.close()
            self._reload(); self.on_changed_callback()

    def _export(self):
        try:
            db = IndexDB(self.db_path); db.export_synonyms_to_json(SYNONYMS_JSON); db.close()
            messagebox.showinfo("완료",f"{SYNONYMS_JSON}로 내보냈습니다.")
        except Exception as e: messagebox.showerror("실패",str(e))

    def _import(self):
        try:
            db = IndexDB(self.db_path); db.import_synonyms_from_json(SYNONYMS_JSON); db.close()
            messagebox.showinfo("완료",f"{SYNONYMS_JSON}에서 가져왔습니다.")
            self._reload(); self.on_changed_callback()
        except Exception as e: messagebox.showerror("실패",str(e))

# ══════════════════════════════════════════════
#  Viewer settings dialog  (modern style)
# ══════════════════════════════════════════════
class ViewerSettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, on_save):
        super().__init__(parent)
        self.title("뷰어 설정")
        self.geometry("680x300")
        self.configure(bg=C["bg"])
        self._settings = dict(settings)
        self._on_save  = on_save
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg=C["sidebar"], padx=18, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="PDF 뷰어 설정", bg=C["sidebar"], fg=C["text"],
                 font=pfont(SANS,13,"bold")).pack(side="left")

        body = tk.Frame(self, bg=C["bg"], padx=22, pady=16)
        body.pack(fill="both", expand=True)

        def row(lbl_text, widget_fn, r):
            tk.Label(body, text=lbl_text, bg=C["bg"], fg=C["text2"],
                     font=pfont(SANS,11), width=18, anchor="w").grid(row=r,column=0,sticky="w",pady=6)
            widget_fn(r)

        self.mode_var  = tk.StringVar(value=self._settings.get("viewer_mode",""))
        self.vpath_var = tk.StringVar(value=self._settings.get("viewer_path",""))
        self.cargs_var = tk.StringVar(value=json.dumps(
            self._settings.get("viewer_custom_args",["{viewer}","{file}"]),ensure_ascii=False))

        def mk_mode(r):
            cb = ttk.Combobox(body, textvariable=self.mode_var, state="readonly",
                               values=["","sumatra","acrobat","custom"], width=14)
            cb.grid(row=r,column=1,sticky="w",padx=6)
        def mk_path(r):
            tk.Entry(body, textvariable=self.vpath_var, width=46, bd=0, relief="flat",
                     bg=C["input_bg"], fg=C["text"], font=pfont(SANS,11),
                     insertbackground=C["accent"]).grid(row=r,column=1,sticky="w",padx=6,ipady=4,ipadx=4)
            FlatButton(body,"찾기",self._pick_exe,small=True).grid(row=r,column=2,padx=4)
        def mk_args(r):
            tk.Entry(body, textvariable=self.cargs_var, width=46, bd=0, relief="flat",
                     bg=C["input_bg"], fg=C["text"], font=pfont(MONO,10),
                     insertbackground=C["accent"]).grid(row=r,column=1,sticky="w",padx=6,ipady=4,ipadx=4)

        row("뷰어 모드", mk_mode, 0)
        row("뷰어 exe 경로", mk_path, 1)
        row("Custom args (JSON)", mk_args, 2)

        tk.Label(body, text='예) ["{viewer}", "-page", "{page}", "{file}"]',
                 bg=C["bg"], fg=C["text3"], font=pfont(SANS,10))\
          .grid(row=3,column=1,sticky="w",padx=6)

        btn_row = tk.Frame(body, bg=C["bg"]); btn_row.grid(row=4,column=1,sticky="e",pady=(16,0))
        FlatButton(btn_row,"취소",self.destroy,small=True).pack(side="left",padx=(0,8))
        FlatButton(btn_row,"저장",self._save,accent=True,small=True).pack(side="left")

    def _pick_exe(self):
        p = filedialog.askopenfilename(filetypes=[("EXE","*.exe"),("All","*.*")])
        if p: self.vpath_var.set(p)

    def _save(self):
        self._settings["viewer_mode"]  = self.mode_var.get().strip()
        self._settings["viewer_path"]  = self.vpath_var.get().strip()
        try:
            args = json.loads(self.cargs_var.get().strip())
            if not isinstance(args,list): raise ValueError
            self._settings["viewer_custom_args"] = args
        except:
            messagebox.showwarning("형식 오류","custom args는 JSON 배열이어야 합니다."); return
        self._on_save(self._settings)
        self.destroy()

# ══════════════════════════════════════════════
#  Main Application Window
# ══════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1240x820")
        self.minsize(1000,660)
        self.configure(bg=C["bg"])

        self.db_path      = os.path.join(os.getcwd(), DB_FILENAME)
        self.event_q      = queue.Queue()
        self.cancel_event = threading.Event()
        self.indexer: Optional[IndexerThread] = None
        self.syn_graph: Dict[str,Set[str]] = {}
        self.settings     = self._load_settings()
        self._result_map: Dict[str,Tuple[str,int]] = {}

        self._setup_ttk_styles()
        self._build_ui()
        self._ensure_db()
        self._load_synonyms()
        self._poll_events()

    # ── settings ──────────────────────────────
    def _load_settings(self):
        if not os.path.exists(SETTINGS_JSON):
            s = {"viewer_mode":"","viewer_path":"","viewer_custom_args":["{viewer}","{file}"]}
            with open(SETTINGS_JSON,"w",encoding="utf-8") as f: json.dump(s,f,ensure_ascii=False,indent=2)
            return s
        try:
            with open(SETTINGS_JSON,"r",encoding="utf-8") as f: return json.load(f) or {}
        except: return {}

    def _save_settings(self, new_settings):
        self.settings = new_settings
        try:
            with open(SETTINGS_JSON,"w",encoding="utf-8") as f:
                json.dump(self.settings,f,ensure_ascii=False,indent=2)
        except Exception as e: messagebox.showwarning("저장 실패",str(e))

    def _ensure_db(self):
        try: db=IndexDB(self.db_path); db.close()
        except Exception as e: messagebox.showerror("DB 초기화 실패",str(e)); self.destroy()

    def _load_synonyms(self):
        try:
            db = IndexDB(self.db_path)
            db.import_synonyms_from_json(SYNONYMS_JSON)  # JSON 파일 동의어
            db.seed_bible_synonyms()                      # 성경 약어 사전 자동 등록
            mp = db.load_synonyms_map(); db.close()
            self.syn_graph = build_graph(mp)
            self._syn_lbl.config(text=f"동의어 {len(mp)}개 term 로드됨")
        except Exception as e:
            self.syn_graph = {}
            self._syn_lbl.config(text=f"동의어 로드 실패: {e}")

    # ── TTK styles ────────────────────────────
    def _setup_ttk_styles(self):
        s = ttk.Style(); s.theme_use("clam")
        s.configure("R.Treeview",
                    background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["text"], rowheight=42, borderwidth=0,
                    font=pfont(SANS,11))
        s.configure("R.Treeview.Heading",
                    background=C["sidebar"], foreground=C["text2"],
                    relief="flat", font=pfont(SANS,10), borderwidth=0)
        s.map("R.Treeview",
              background=[("selected",C["accent_dim"])],
              foreground=[("selected",C["accent_text"])])

    # ── Build UI ──────────────────────────────
    def _build_ui(self):
        # ─ Sidebar ─
        sb = tk.Frame(self, bg=C["sidebar"], width=232)
        sb.pack(side="left", fill="y"); sb.pack_propagate(False)

        # logo
        lf = tk.Frame(sb, bg=C["sidebar"], padx=16, pady=16); lf.pack(fill="x")
        lc = tk.Canvas(lf, width=28, height=28, bg=C["sidebar"], highlightthickness=0)
        lc.pack(side="left")
        lc.create_oval(0,0,28,28, fill=C["accent"], outline="")
        lc.create_text(14,14, text="P", fill="white", font=pfont(SANS,13,"bold"))
        tk.Label(lf, text="PDF Search", bg=C["sidebar"], fg=C["text"],
                 font=pfont(SANS,14,"bold")).pack(side="left", padx=10)

        tk.Frame(sb, bg=C["border"], height=1).pack(fill="x", padx=14)

        # folder
        fs = tk.Frame(sb, bg=C["sidebar"], padx=14, pady=14); fs.pack(fill="x")
        tk.Label(fs, text="대상 폴더", bg=C["sidebar"], fg=C["text3"],
                 font=pfont(SANS,9)).pack(anchor="w", pady=(0,5))
        self.folder_var = tk.StringVar()
        tk.Entry(fs, textvariable=self.folder_var, bd=0, relief="flat",
                 bg=C["input_bg"], fg=C["text"], font=pfont(SANS,11),
                 insertbackground=C["accent"]).pack(fill="x", ipady=5, ipadx=6)
        FlatButton(fs, "📂  폴더 선택", self.on_pick_folder, small=True)\
            .pack(fill="x", pady=(8,0))

        tk.Frame(sb, bg=C["border"], height=1).pack(fill="x", padx=14)

        # index options
        io = tk.Frame(sb, bg=C["sidebar"], padx=14, pady=14); io.pack(fill="x")
        tk.Label(io, text="인덱싱 옵션", bg=C["sidebar"], fg=C["text3"],
                 font=pfont(SANS,9)).pack(anchor="w", pady=(0,10))
        self.build_ngram_var = tk.BooleanVar(value=False)
        self.parallel_var    = tk.BooleanVar(value=False)
        self.workers_var     = tk.IntVar(value=min(4, os.cpu_count() or 2))
        for txt, var in [("N-gram 인덱스", self.build_ngram_var),
                          ("병렬 추출",    self.parallel_var)]:
            Toggle(io, txt, var).pack(anchor="w", pady=4)
        wr = tk.Frame(io, bg=C["sidebar"]); wr.pack(fill="x", pady=(8,0))
        tk.Label(wr, text="Workers", bg=C["sidebar"], fg=C["text2"],
                 font=pfont(SANS,10)).pack(side="left")
        tk.Spinbox(wr, from_=1, to=max(1,os.cpu_count() or 2),
                   textvariable=self.workers_var, width=4,
                   bg=C["input_bg"], fg=C["text"], bd=0,
                   buttonbackground=C["panel2"], font=pfont(SANS,10))\
            .pack(side="right")

        # action buttons
        ab = tk.Frame(sb, bg=C["sidebar"], padx=14); ab.pack(fill="x")
        FlatButton(ab,"인덱싱 시작",self.on_start_index,accent=True)\
            .pack(fill="x", pady=(0,6))
        FlatButton(ab,"취소",self.on_cancel_index)\
            .pack(fill="x")

        tk.Frame(sb, bg=C["border"], height=1).pack(fill="x", padx=14, pady=12)

        # nav
        nav = tk.Frame(sb, bg=C["sidebar"], padx=10); nav.pack(fill="x")
        self._nav_btn(nav, "📖  동의어 관리", self.on_manage_synonyms)
        self._nav_btn(nav, "⚙   뷰어 설정",  self.on_viewer_settings)

        self._syn_lbl = tk.Label(sb, text="", bg=C["sidebar"], fg=C["text3"],
                                  font=pfont(SANS,9), wraplength=200, justify="left")
        self._syn_lbl.pack(side="bottom", padx=14, pady=10, anchor="w")

        # ─ Main content ─
        main = tk.Frame(self, bg=C["bg"]); main.pack(side="left", fill="both", expand=True)

        # top bar: search
        tb = tk.Frame(main, bg=C["panel"], padx=18, pady=14); tb.pack(fill="x")
        sr = tk.Frame(tb, bg=C["panel"]); sr.pack(fill="x")
        self.query_var = tk.StringVar()
        self._sbar = SearchBar(sr, textvariable=self.query_var,
                                placeholder='검색어 입력… (구문은 "따옴표" 사용)')
        self._sbar.pack(side="left", fill="x", expand=True)
        self._sbar.entry.bind("<Return>", lambda e: self.on_search())
        FlatButton(sr, "검색", self.on_search, accent=True)\
            .pack(side="left", padx=(10,0))

        # filter row
        fr = tk.Frame(tb, bg=C["panel"]); fr.pack(fill="x", pady=(10,0))
        self.expand_var       = tk.BooleanVar(value=False)
        self.ngram_search_var = tk.BooleanVar(value=False)
        for txt, var in [("동의어 확장", self.expand_var),
                          ("N-gram 검색", self.ngram_search_var)]:
            t = Toggle(fr, txt, var); t.pack(side="left", padx=(0,20))

        # progress strip
        self._prog = ProgressStrip(main); self._prog.pack(fill="x")

        # status bar
        stbar = tk.Frame(main, bg=C["panel"], padx=18, pady=7); stbar.pack(fill="x")
        self._sdot = StatusDot(stbar); self._sdot.pack(side="left")
        self._stxt = tk.Label(stbar, text="폴더를 선택하고 인덱싱을 시작하세요.",
                               bg=C["panel"], fg=C["text2"], font=pfont(SANS,11))
        self._stxt.pack(side="left", padx=8)
        self._rcnt = tk.Label(stbar, text="", bg=C["panel"],
                               fg=C["text3"], font=pfont(SANS,10))
        self._rcnt.pack(side="right")

        # results tree
        rf = tk.Frame(main, bg=C["bg"]); rf.pack(fill="both", expand=True)
        cols = ("path","page","snippet")
        self.tree = ttk.Treeview(rf, columns=cols, show="headings", style="R.Treeview")
        self.tree.heading("path",    text="파일 경로")
        self.tree.heading("page",    text="페이지")
        self.tree.heading("snippet", text="미리보기")
        self.tree.column("path",    width=480, anchor="w")
        self.tree.column("page",    width=66,  anchor="center")
        self.tree.column("snippet", width=560, anchor="w")
        vsb = ttk.Scrollbar(rf, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(rf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0,column=0,sticky="nsew")
        vsb.grid(row=0,column=1,sticky="ns")
        hsb.grid(row=1,column=0,sticky="ew")
        rf.grid_rowconfigure(0,weight=1); rf.grid_columnconfigure(0,weight=1)
        self.tree.tag_configure("odd",  background=C["panel"])
        self.tree.tag_configure("even", background=C["panel2"])
        self.tree.bind("<Double-1>", self.on_open_selected)

        # error panel (collapsible)
        self._err_open = False
        et = tk.Frame(main, bg=C["sidebar"], padx=16, pady=5, cursor="hand2"); et.pack(fill="x")
        self._etgl = tk.Label(et, text="▶  오류 로그", bg=C["sidebar"],
                               fg=C["text3"], font=pfont(SANS,10), cursor="hand2")
        self._etgl.pack(side="left")
        et.bind("<Button-1>", self._toggle_err)
        self._etgl.bind("<Button-1>", self._toggle_err)
        self._err_frame = tk.Frame(main, bg=C["sidebar"])
        self.err_text = tk.Text(self._err_frame, height=5, wrap="word",
                                 bg=C["red_bg"], fg=C["red"],
                                 font=pfont(MONO,10), bd=0, relief="flat",
                                 padx=16, pady=10)
        self.err_text.pack(fill="both", expand=True)

    def _nav_btn(self, parent, label, cmd):
        f = tk.Frame(parent, bg=C["sidebar"], cursor="hand2"); f.pack(fill="x", pady=2)
        inner = tk.Frame(f, bg=C["sidebar"], padx=10, pady=7); inner.pack(fill="x")
        tk.Label(inner, text=label, bg=C["sidebar"], fg=C["text2"],
                 font=pfont(SANS,11)).pack(side="left")
        def on_enter(e): inner.config(bg=C["hover"])
        def on_leave(e): inner.config(bg=C["sidebar"])
        for w in (f, inner) + inner.winfo_children():
            w.bind("<Enter>", on_enter); w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", lambda e, c=cmd: c())

    def _toggle_err(self, e=None):
        if self._err_open:
            self._err_frame.pack_forget(); self._etgl.config(text="▶  오류 로그")
        else:
            self._err_frame.pack(fill="x"); self._etgl.config(text="▼  오류 로그")
        self._err_open = not self._err_open

    def _set_status(self, msg, status="idle"):
        self._sdot.set_status(status)
        self._stxt.config(text=msg)

    # ── Event handlers ────────────────────────
    def on_pick_folder(self):
        f = filedialog.askdirectory(title="PDF 폴더 선택")
        if f: self.folder_var.set(f)

    def on_manage_synonyms(self):
        SynonymsDialog(self, self.db_path, self._load_synonyms)

    def on_viewer_settings(self):
        ViewerSettingsDialog(self, self.settings, self._save_settings)

    def on_start_index(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("폴더 필요","유효한 폴더를 선택하세요."); return
        if self.indexer and self.indexer.is_alive():
            messagebox.showinfo("실행 중","인덱싱이 이미 실행 중입니다."); return
        self.err_text.delete("1.0","end")
        self._prog.set(0); self.cancel_event.clear()
        self._set_status("스캔 중…","running")
        self.indexer = IndexerThread(
            db_path=self.db_path, root_folder=folder,
            event_queue=self.event_q, cancel_event=self.cancel_event,
            build_ngram=self.build_ngram_var.get(),
            parallel=self.parallel_var.get(),
            max_workers=int(self.workers_var.get()),
        )
        self.indexer.start()

    def on_cancel_index(self):
        if self.indexer and self.indexer.is_alive():
            self.cancel_event.set(); self._set_status("취소 요청됨…","running")
        else:
            self._set_status("실행 중인 인덱싱이 없습니다.","idle")

    def on_search(self):
        user_q = self._sbar.get().strip()
        if not user_q: messagebox.showinfo("검색어 없음","검색어를 입력하세요."); return
        try:
            db = IndexDB(self.db_path)
            fts_q = make_fts_query(user_q, self.expand_var.get(), self.syn_graph)
            rows  = db.search_text(fts_q, 250) if fts_q else []
            ng_rows = []
            if self.ngram_search_var.get():
                ng_q = make_ngram_query(user_q, self.expand_var.get(), self.syn_graph)
                if ng_q: ng_rows = db.search_ngram(ng_q, 250)
            db.close()

            merged: Dict[Tuple[str,int],Tuple[str,float]] = {}
            for r in rows:
                merged[(r["path"],int(r["page_no"]))] = (r["snippet"] or "", float(r["score"]))
            for r in ng_rows:
                key = (r["path"],int(r["page_no"]))
                if key not in merged:
                    merged[key] = (r["snippet"] or "", float(r["score"])+2.0)

            top = nsmallest(500, merged.items(), key=lambda kv: kv[1][1])
            for it in self.tree.get_children(): self.tree.delete(it)
            self._result_map = {}
            for i, ((path, page), (snip, _)) in enumerate(top):
                tag = "odd" if i%2==0 else "even"
                fname = os.path.basename(path)
                disp_path = fname + "   " + os.path.dirname(path)
                iid = self.tree.insert("","end", values=(disp_path, page, snip), tags=(tag,))
                self._result_map[iid] = (path, page)

            self._rcnt.config(text=f"{len(merged):,}건 검색됨")
            self._set_status(f"검색 완료 — {len(merged):,}건", "ok")
        except Exception as e:
            messagebox.showerror("검색 실패",str(e))

    def on_open_selected(self, _evt=None):
        sel = self.tree.selection()
        if not sel: return
        path, page = self._result_map.get(sel[0], (None,1))
        if not path: return
        try: open_pdf_at_page(path, page, self.settings)
        except Exception as e: messagebox.showerror("열기 실패",str(e))

    # ── Event polling ─────────────────────────
    def _poll_events(self):
        try:
            while True:
                typ, payload = self.event_q.get_nowait()
                if typ == "scan":
                    self._prog.set(0)
                    self._set_status(f"스캔 완료. 총 {payload}개","running")
                elif typ == "plan":
                    c,t = payload; self._set_status(f"변경 {c}개 / 총 {t}개 처리 중…","running")
                elif typ == "parallel":
                    self._set_status(f"병렬 추출 (workers={payload})","running")
                elif typ == "progress":
                    p = payload
                    self._prog.set((p.current/max(1,p.total))*100)
                    self._set_status(f"[{p.current}/{p.total}]  {os.path.basename(p.path)}","running")
                elif typ == "extract_done":
                    d,t,path = payload
                    self._prog.set((d/max(1,t))*100)
                    self._set_status(f"[{d}/{t}]  {os.path.basename(path)}","running")
                elif typ == "deleted":
                    self._set_status(f"삭제 반영: {os.path.basename(payload)}","running")
                elif typ == "error":
                    err = payload
                    if not self._err_open: self._toggle_err()
                    self.err_text.insert("end",f"✕  {err.path}\n   {err.error}\n\n")
                    self.err_text.see("end")
                elif typ == "errors_done":
                    errs = payload
                    self._prog.set(100)
                    self._set_status(f"완료 (오류 {len(errs)}건)","error") if errs \
                        else self._set_status("인덱싱 완료","ok")
                elif typ == "canceled":
                    self._prog.set(0); self._set_status("인덱싱 취소됨","idle")
                elif typ == "done":
                    self._prog.set(100); self._set_status("인덱싱 완료. 검색 가능.","ok")
                elif typ == "fatal":
                    messagebox.showerror("치명적 오류",str(payload))
                    self._set_status("오류로 중단됨","error")
        except queue.Empty:
            pass
        finally:
            self.after(120, self._poll_events)


def main():
    if fitz is None:
        print("PyMuPDF가 필요합니다: pip install pymupdf")
    app = App(); app.mainloop()

if __name__ == "__main__":
    main()
