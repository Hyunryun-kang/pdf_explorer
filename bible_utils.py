import re

# ==============================
# 📖 성경 약어 테이블
# ==============================

BIBLE_BOOK_MAP = {
    # 구약
    "창": "창세기",
    "출": "출애굽기",
    "레": "레위기",
    "민": "민수기",
    "신": "신명기",
    "수": "여호수아",
    "삿": "사사기",
    "룻": "룻기",
    "삼상": "사무엘상",
    "삼하": "사무엘하",
    "왕상": "열왕기상",
    "왕하": "열왕기하",
    "대상": "역대상",
    "대하": "역대하",
    "스": "에스라",
    "느": "느헤미야",
    "에": "에스더",
    "욥": "욥기",
    "시": "시편",
    "잠": "잠언",
    "전": "전도서",
    "아": "아가",
    "사": "이사야",
    "렘": "예레미야",
    "애": "예레미야애가",
    "겔": "에스겔",
    "단": "다니엘",
    "호": "호세아",
    "욜": "요엘",
    "암": "아모스",
    "옵": "오바댜",
    "욘": "요나",
    "미": "미가",
    "나": "나훔",
    "합": "하박국",
    "습": "스바냐",
    "학": "학개",
    "슥": "스가랴",
    "말": "말라기",

    # 신약
    "마": "마태복음",
    "막": "마가복음",
    "눅": "누가복음",
    "요": "요한복음",
    "행": "사도행전",
    "롬": "로마서",
    "고전": "고린도전서",
    "고후": "고린도후서",
    "갈": "갈라디아서",
    "엡": "에베소서",
    "빌": "빌립보서",
    "골": "골로새서",
    "살전": "데살로니가전서",
    "살후": "데살로니가후서",
    "딤전": "디모데전서",
    "딤후": "디모데후서",
    "딛": "디도서",
    "몬": "빌레몬서",
    "히": "히브리서",
    "약": "야고보서",
    "벧전": "베드로전서",
    "벧후": "베드로후서",
    "요일": "요한일서",
    "요이": "요한이서",
    "요삼": "요한삼서",
    "유": "유다서",
    "계": "요한계시록",
}

FULL_TO_SHORT = {v: k for k, v in BIBLE_BOOK_MAP.items()}


# ==============================
# 📖 Book Normalize (강화)
# ==============================
def normalize_book(book):

    # full → short
    if book in FULL_TO_SHORT:
        return FULL_TO_SHORT[book]

    # short 그대로
    if book in BIBLE_BOOK_MAP:
        return book

    # "마태" → "마"
    for full, short in FULL_TO_SHORT.items():
        if book.startswith(full):
            return short

    return None


# ==============================
# 📖 Query 파싱 (완전 강화)
# ==============================
def parse_bible_query(query):

    # 공백 제거
    q = re.sub(r"\s+", "", query)

    # --------------------------
    # 1️⃣ 범위 (마20:10-12 / 마태복음20장10절~12절)
    # --------------------------
    match = re.search(
        r"([가-힣]+?)(\d+)[장:]?(\d+)[절:]?[-~](\d+)",
        q
    )

    if match:
        book, ch, start, end = match.groups()
        book = normalize_book(book)
        if not book:
            return None

        return {
            "book": book,
            "chapter": int(ch),
            "verse_start": int(start),
            "verse_end": int(end),
            "mode": "range"
        }

    # --------------------------
    # 2️⃣ 단일 절 (마20:10 / 마태복음20장10절)
    # --------------------------
    match = re.search(
        r"([가-힣]+?)(\d+)[장:]?(\d+)[절]?",
        q
    )

    if match:
        book, ch, vs = match.groups()
        book = normalize_book(book)
        if not book:
            return None

        return {
            "book": book,
            "chapter": int(ch),
            "verse_start": int(vs),
            "verse_end": int(vs),
            "mode": "single"
        }

    # --------------------------
    # 3️⃣ 장만 (마20장 / 마태복음20장)
    # --------------------------
    match = re.search(
        r"([가-힣]+?)(\d+)장",
        q
    )

    if match:
        book, ch = match.groups()
        book = normalize_book(book)
        if not book:
            return None

        return {
            "book": book,
            "chapter": int(ch),
            "mode": "chapter"
        }

    # --------------------------
    # 4️⃣ 책 전체
    # --------------------------
    book = normalize_book(q)
    if book:
        return {
            "book": book,
            "mode": "book"
        }

    return None