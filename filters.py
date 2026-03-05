import re

def parse_query_conditions(query: str):

    query_lower = query.lower()

    conditions = {
        "type": None,        # scripture | content | image
        "top_k": 3,
        "raw_query": query
    }

    # 🔹 개수 추출
    num_match = re.search(r"\d+", query)
    if num_match:
        conditions["top_k"] = int(num_match.group())

    # 🔹 type 필터
    if "그림" in query_lower or "이미지" in query_lower:
        conditions["type"] = "image"

    elif "말씀" in query_lower or "본문" in query_lower:
        conditions["type"] = "scripture"

    elif "설교" in query_lower or "내용" in query_lower:
        conditions["type"] = "content"

    return conditions