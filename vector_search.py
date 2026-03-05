import numpy as np
from collections import defaultdict
from utils import embedder

def vector_search(query, index, metadata, conditions):

    query_emb = embedder.encode(
        query,
        normalize_embeddings=True
    ).astype("float32")

    D, I = index.search(np.array([query_emb]), 100)

    exact_matches = []
    normal_results = []

    for score, idx in zip(D[0], I[0]):

        meta = metadata[idx]

        # type 필터
        if conditions["type"]:
            if meta["type"] != conditions["type"]:
                continue

        # 🔥 성경 구절 정확 매칭 우선
        if meta["type"] == "scripture":
            if meta.get("verse","") in query:
                exact_matches.append((score + 10, meta))
                continue

        normal_results.append((score, meta))

    results = exact_matches + normal_results

    results = sorted(results, key=lambda x: x[0], reverse=True)

    return results[:conditions["top_k"]]