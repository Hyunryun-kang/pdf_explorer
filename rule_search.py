def rule_search(query, metadata, conditions):

    tokens = query.lower().split()
    results = []

    for meta in metadata:

        if conditions["type"]:
            if meta["type"] != conditions["type"]:
                continue

        combined = (
            meta.get("title","") + " " +
            meta.get("verse","") + " " +
            meta.get("text","")
        ).lower()

        score = sum(t in combined for t in tokens)

        if score > 0:
            results.append((score, meta))

    results = sorted(results, key=lambda x: x[0], reverse=True)

    return results[:conditions["top_k"]]