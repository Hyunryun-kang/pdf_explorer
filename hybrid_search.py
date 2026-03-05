from search.bible_utils import parse_bible_query
import numpy as np
import re
from collections import defaultdict


# ==================================================
# 📖 말씀 전용 검색 (전체 검사 + Boolean 날짜)
# ==================================================
def verse_search(query, metadata):

    parsed = parse_bible_query(query)
    target_year = extract_year_from_query(query)

    if not parsed:
        return [], {}

    grouped = defaultdict(list)

    for meta in metadata:

        if meta.get("type") != "inline_verse":
            continue

        # =========================
        # 🔥 날짜 Boolean
        # =========================
        if target_year is not None:
            if meta.get("year") != target_year:
                continue

        # =========================
        # 🔥 책 일치
        # =========================
        if meta.get("book") != parsed["book"]:
            continue

        mode = parsed["mode"]

        # =========================
        # 🔥 책 전체
        # =========================
        if mode == "book":
            grouped[meta["file"]].append((1.0, meta))
            continue

        # =========================
        # 🔥 장 검색
        # =========================
        if mode == "chapter":

            if meta.get("chapter") == parsed["chapter"]:
                grouped[meta["file"]].append((1.0, meta))

            continue

        # =========================
        # 🔥 단일절
        # =========================
        if mode == "single":

            if meta.get("chapter") != parsed["chapter"]:
                continue

            q_verse = parsed["verse_start"]

            m_start = meta.get("verse_start")
            m_end   = meta.get("verse_end", m_start)

            if m_start is None:
                continue

            # 포함 관계
            if m_start <= q_verse <= m_end:
                grouped[meta["file"]].append((1.0, meta))

            continue

        # =========================
        # 🔥 범위 검색
        # =========================
        if mode == "range":

            if meta.get("chapter") != parsed["chapter"]:
                continue

            q_start = parsed["verse_start"]
            q_end   = parsed["verse_end"]

            m_start = meta.get("verse_start")
            m_end   = meta.get("verse_end", m_start)

            if m_start is None:
                continue

            # 범위 겹침
            if max(q_start, m_start) <= min(q_end, m_end):
                grouped[meta["file"]].append((1.0, meta))

            continue

    # =========================
    # 🔥 전체 반환 (Top-K 없음)
    # =========================
    file_rank = sorted(
        [(len(items), file) for file, items in grouped.items()],
        reverse=True
    )

    return file_rank, grouped


# ==================================================
# 🔎 Hybrid Search (일반 검색)
# ==================================================
def hybrid_search(query, index, metadata, bm25, conditions):

    # 🔥 말씀 전용 모드 → FAISS 사용 안 함
    if conditions.get("type") == "inline_verse":
        return verse_search(query, metadata)

    try:
        from utils import embedder

        top_k = conditions.get("top_k", 5)
        search_type = conditions.get("type")

        target_year = extract_year_from_query(query)
        parsed_verse = parse_bible_query(query)

        # Query embedding
        q_emb = embedder.encode(
            query,
            normalize_embeddings=True
        ).astype("float32")

        # 🔥 충분한 후보 확보 (100k면 2000 추천)
        candidate_k = min(2000, index.ntotal)
        D, I = index.search(np.array([q_emb]), candidate_k)

        file_chunk_scores = defaultdict(list)
        file_chunk_meta = defaultdict(list)

        for score, idx in zip(D[0], I[0]):

            meta = metadata[idx]

            # UI type 필터
            if search_type is not None:
                if meta.get("type") != search_type:
                    continue

            file = meta["file"]

            file_chunk_scores[file].append(float(score))
            file_chunk_meta[file].append((float(score), meta))

        file_scores = {}
        grouped = defaultdict(list)

        for file, scores in file_chunk_scores.items():

            # 🔥 날짜 Boolean
            if target_year is not None:
                file_year = get_file_year(metadata, file)
                if file_year != target_year:
                    continue

            # 🔥 말씀 Boolean (범위 겹침)
            if parsed_verse:
                if not file_contains_verse(metadata, file, parsed_verse):
                    continue

            # 🔥 Top-5 평균
            k = min(5, len(scores))
            top_scores = sorted(scores, reverse=True)[:k]
            final_score = sum(top_scores) / k

            file_scores[file] = final_score

            grouped[file] = sorted(
                file_chunk_meta[file],
                key=lambda x: x[0],
                reverse=True
            )[:5]

        file_rank = sorted(
            [(score, file) for file, score in file_scores.items()],
            reverse=True
        )[:top_k]

        return file_rank, grouped

    except Exception as e:
        print("Hybrid search error:", e)
        return [], {}


# ==================================================
# 📅 연도 추출
# ==================================================
def extract_year_from_query(query):
    match = re.search(r"(20\d{2})", query)
    if match:
        return int(match.group(1))
    return None


# ==================================================
# 📅 파일 연도 조회 (최적화 가능)
# ==================================================
def get_file_year(metadata, file):
    for meta in metadata:
        if meta["file"] == file and "year" in meta:
            return meta["year"]
    return None


# ==================================================
# 📖 파일 내 말씀 존재 여부 (범위 겹침 적용)
# ==================================================
def file_contains_verse(metadata, file, parsed_verse):

    for meta in metadata:

        if meta["file"] != file:
            continue

        if meta.get("type") != "inline_verse":
            continue

        if meta.get("book") != parsed_verse["book"]:
            continue

        if parsed_verse.get("chapter") and meta.get("chapter") != parsed_verse["chapter"]:
            continue

        if parsed_verse["mode"] in ["single", "range"]:

            q_start = parsed_verse["verse_start"]
            q_end   = parsed_verse["verse_end"]

            m_start = meta.get("verse_start")
            m_end   = meta.get("verse_end")

            if m_start is None:
                continue

            if max(q_start, m_start) <= min(q_end, m_end):
                return True

        else:
            return True

    return False