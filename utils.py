import requests
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity


# =========================
# 1. Load Embedder
# =========================
embedder = SentenceTransformer(
    "jhgan/ko-sroberta-multitask",
    device="cpu"   # GPU 있으면 "cuda"
)


# =========================
# 2. Chunking
# =========================
def chunk_text(text, chunk_size=500, overlap=100):
    words = text.split()
    chunks = []

    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)

    return chunks


# =========================
# 3. Build Vector DB
# =========================
def build_vector_db(text):
    chunks = chunk_text(text)
    vectors = embedder.encode(
        chunks,
        normalize_embeddings=True
    )
    return chunks, vectors


# =========================
# 4. Retrieve Top-K
# =========================
def retrieve(question, chunks, vectors, top_k=3):
    query_vec = embedder.encode(
        [question],
        normalize_embeddings=True
    )

    scores = cosine_similarity(query_vec, vectors)[0]
    top_k_idx = scores.argsort()[-top_k:][::-1]

    retrieved_chunks = [chunks[i] for i in top_k_idx]
    return "\n\n".join(retrieved_chunks)


# =========================
# 5. Build Prompt
# =========================
def build_prompt(context, question):
    return f"""
너는 문서 기반 질의응답 시스템이다.
반드시 아래 Context 안의 정보만 사용해서 답하라.
Context에 없는 내용은 추측하지 말고
'문서에 없습니다.'라고 답하라.

Context:
{context}

Question:
{question}

Answer:
"""


# =========================
# 6. Call Ollama
# =========================
def call_ollama(prompt, model="mistral"):
    url = "http://localhost:11434/api/generate"

    response = requests.post(
        url,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1
            }
        }
    )

    return response.json()["response"]