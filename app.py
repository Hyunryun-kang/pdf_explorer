import streamlit as st
import pickle
import faiss
import os
import numpy as np

from search.filters import parse_query_conditions
from search.hybrid_search import hybrid_search
from search.followup import answer_followup
from search.bible_utils import parse_bible_query
from utils import embedder
from indexer import index_single_pdf  # 🔥 v3 사용

INDEX_FILE = "faiss.index"
META_FILE = "metadata.pkl"
BM25_FILE = "bm25.pkl"
PDF_FOLDER = "pdfs_all"

# =========================
# 페이지 설정
# =========================
st.set_page_config(page_title="설교 검색 시스템", layout="wide")
st.title("📖 설교 검색 시스템")

# =========================
# 세션 상태
# =========================
if "last_grouped" not in st.session_state:
    st.session_state.last_grouped = None

if "last_file_rank" not in st.session_state:
    st.session_state.last_file_rank = None

# =========================
# 인덱스 로딩
# =========================
@st.cache_resource
def load_index():
    index = faiss.read_index(INDEX_FILE)

    with open(META_FILE, "rb") as f:
        metadata = pickle.load(f)

    with open(BM25_FILE, "rb") as f:
        bm25 = pickle.load(f)

    return index, metadata, bm25


index, metadata, bm25 = load_index()

# ==================================================
# 🔥 PDF 업로드 → Incremental 인덱싱
# ==================================================
st.divider()
st.subheader("📤 새 PDF 업로드하여 검색 DB에 추가")

uploaded_file = st.file_uploader("PDF 업로드", type=["pdf"])

if uploaded_file:

    save_path = os.path.join(PDF_FOLDER, uploaded_file.name)

    with open(save_path, "wb") as f:
        f.write(uploaded_file.read())

    with st.spinner("📚 인덱싱 중..."):

        index = faiss.read_index(INDEX_FILE)

        with open(META_FILE, "rb") as f:
            metadata = pickle.load(f)

        with open(BM25_FILE, "rb") as f:
            bm25 = pickle.load(f)

        existing_hashes = set(
            m.get("file_hash")
            for m in metadata
            if m.get("file_hash")
        )

        new_vectors, new_meta, new_tokens, file_hash = index_single_pdf(
            save_path,
            uploaded_file.name,
            existing_hashes
        )

        if len(new_vectors) == 0:
            st.warning("⚠ 이미 인덱싱된 파일이거나 데이터가 없습니다.")
            st.stop()

        new_vectors = np.array(new_vectors).astype("float32")

        index.add(new_vectors)
        metadata.extend(new_meta)

        all_tokens = bm25.corpus + new_tokens
        new_bm25 = BM25Okapi(all_tokens)

        faiss.write_index(index, INDEX_FILE)

        with open(META_FILE, "wb") as f:
            pickle.dump(metadata, f)

        with open(BM25_FILE, "wb") as f:
            pickle.dump(new_bm25, f)

    st.cache_resource.clear()
    st.success("✅ 인덱싱 완료! 검색에 반영되었습니다.")
    st.experimental_rerun()

# ==================================================
# 🔎 검색 옵션 UI
# ==================================================
st.divider()

col1, col2 = st.columns([2, 1])

with col1:
    search_mode = st.radio(
        "검색 범위 선택",
        ["전체", "📖 말씀만", "📝 설교 내용만", "🖼 그림만"],
        horizontal=True
    )

with col2:
    top_k = st.number_input(
        "결과 개수",
        min_value=1,
        max_value=100,
        value=3,
        step=1
    )

if search_mode == "📖 말씀만":
    sort_mode = st.radio(
        "정렬 기준 선택",
        ["📅 날짜순 (최신 우선)", "📊 인용순"],
        horizontal=True
    )
else:
    sort_mode = None

# ==================================================
# 검색 입력
# ==================================================
query = st.text_input(
    "검색어 입력 (예: 행 1:8 / 사도행전 1장 / 왕따 간증 / 지성소 그림)"
)

# ==================================================
# 흐름 재정렬 함수
# ==================================================
def semantic_rerank_with_highlight(sub_query, file_rank):

    q_emb = embedder.encode(
        sub_query,
        normalize_embeddings=True
    ).astype("float32")

    reranked = []
    highlight_map = {}

    for _, file in file_rank:

        chunk_indices = [
            i for i, m in enumerate(metadata)
            if m["file"] == file and m["type"] == "content"
        ]

        if not chunk_indices:
            continue

        scored_chunks = []

        for idx in chunk_indices:
            vec = index.reconstruct(idx)
            score = float(np.dot(q_emb, vec))
            scored_chunks.append((score, idx))

        if not scored_chunks:
            continue

        top_chunks = sorted(scored_chunks, reverse=True)[:3]
        avg_score = sum(s for s, _ in top_chunks) / len(top_chunks)

        reranked.append((avg_score, file))

        highlight_items = []

        for score, idx in top_chunks:
            meta_chunk = metadata[idx]
            page = meta_chunk.get("page")

            highlight_items.append(meta_chunk)

            for m in metadata:
                if (
                    m["file"] == file and
                    m.get("type") == "inline_verse" and
                    m.get("page") == page
                ):
                    highlight_items.append(m)

        highlight_map[file] = highlight_items

    return sorted(reranked, reverse=True), highlight_map

# ==================================================
# 검색 실행
# ==================================================
if query:

    with st.spinner("🔎 검색 중입니다..."):

        conditions = parse_query_conditions(query)

        if search_mode == "📖 말씀만":
            parsed = parse_bible_query(query)
            if not parsed:
                st.warning("⚠ 성경 형식으로 입력해주세요.")
                st.stop()
            conditions["type"] = "inline_verse"

        elif search_mode == "📝 설교 내용만":
            conditions["type"] = "content"

        elif search_mode == "🖼 그림만":
            conditions["type"] = "image_all"

        else:
            conditions["type"] = None

        conditions["top_k"] = top_k

        file_rank, grouped = hybrid_search(
            query,
            index,
            metadata,
            bm25,
            conditions
        )

        if search_mode == "📖 말씀만" and file_rank:

            if sort_mode == "📅 날짜순 (최신 우선)":

                def get_date(file):
                    items = grouped[file]
                    for _, meta in items:
                        return (
                            meta.get("year") or 0,
                            meta.get("month") or 0,
                            meta.get("day") or 0
                        )
                    return (0, 0, 0)

                file_rank = sorted(
                    file_rank,
                    key=lambda x: get_date(x[1]),
                    reverse=True
                )

            else:
                file_rank = sorted(
                    file_rank,
                    key=lambda x: len(grouped[x[1]]),
                    reverse=True
                )

        st.session_state.last_grouped = grouped
        st.session_state.last_file_rank = file_rank

    if not file_rank:
        st.warning("❌ 검색 결과가 없습니다.")

    else:

        highlight_map = None

        if search_mode == "📖 말씀만":
            st.divider()
            st.subheader("🔎 이 결과 안에서 추가 흐름 검색")

            sub_query = st.text_input("예: 회복 / 제자도 / 은혜 중심")

            if sub_query:
                with st.spinner("🔄 흐름 분석 중..."):
                    file_rank, highlight_map = semantic_rerank_with_highlight(
                        sub_query,
                        file_rank
                    )

        for score, file in file_rank[:top_k]:

            st.divider()

            colA, colB = st.columns([5,1])

            with colA:
                st.header(f"📂 {file}")

            with colB:
                pdf_path = os.path.join(PDF_FOLDER, file)
                if os.path.exists(pdf_path):
                    with open(pdf_path, "rb") as f:
                        st.download_button(
                            "📂 열기",
                            f,
                            file_name=file,
                            key=f"download_{file}"
                        )

            if search_mode == "📖 말씀만":
                st.caption(f"📖 말씀 개수: {len(grouped[file])}")
            else:
                st.caption(f"파일 점수: {round(score,3)}")

            items = highlight_map[file] if highlight_map and file in highlight_map else grouped[file]

            for meta in items:

                if meta["type"] == "inline_verse":
                    st.subheader("📖 말씀")
                    st.markdown(f"### {meta['text']}")
                    if meta.get("page"):
                        st.caption(f"📄 페이지: {meta['page']}")

                elif meta["type"] == "image_caption":
                    st.subheader("🖼 그림")
                    if meta.get("image_path") and os.path.exists(meta["image_path"]):
                        st.image(meta["image_path"], width=300)
                    st.write(meta["text"])

                else:
                    st.subheader("📝 설교 내용")
                    st.write(meta["text"])

# ==================================================
# 💬 후속 질문
# ==================================================
if st.session_state.last_grouped:

    st.divider()
    st.subheader("💬 검색 결과에 대해 질문하기")

    followup_q = st.text_input("후속 질문 입력")

    if followup_q:

        with st.spinner("🤖 답변 생성 중..."):

            context_chunks = []

            for file, items in st.session_state.last_grouped.items():
                for _, meta in items:
                    if "text" in meta:
                        context_chunks.append(meta["text"])

            answer = answer_followup(
                followup_q,
                context_chunks
            )

        st.markdown("### 🤖 답변")
        st.write(answer)