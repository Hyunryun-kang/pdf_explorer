import os
import re
import fitz
import faiss
import torch
import pickle
import hashlib
import numpy as np
from tqdm import tqdm
from PIL import Image
from datetime import datetime
from rank_bm25 import BM25Okapi
from utils import embedder
from transformers import BlipProcessor, BlipForConditionalGeneration

# ==================================================
# ⚙️ 설정
# ==================================================
PDF_FOLDER = "pdfs_all"
INDEX_FILE = "faiss.index"
META_FILE = "metadata.pkl"
BM25_FILE = "bm25.pkl"
IMAGE_DIR = "extracted_images"

MODE = "full"  # "full" | "incremental"

os.makedirs(IMAGE_DIR, exist_ok=True)

torch.set_num_threads(4)
device = "cuda" if torch.cuda.is_available() else "cpu"

# ==================================================
# 🔥 BLIP 모델 로딩 (GPU)
# ==================================================
print("🔄 BLIP 모델 로딩...")
processor = BlipProcessor.from_pretrained(
    "Salesforce/blip-image-captioning-base"
)
model = BlipForConditionalGeneration.from_pretrained(
    "Salesforce/blip-image-captioning-base"
).to(device)
model.eval()

# ==================================================
# 🔑 파일 해시
# ==================================================
def compute_file_hash(path):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        sha.update(f.read())
    return sha.hexdigest()

# ==================================================
# 📄 텍스트 청킹
# ==================================================
def chunk_text(text, chunk_size=300, overlap=80):
    words = text.split()
    chunks = []

    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.strip()) > 30:
            chunks.append(chunk)

    return chunks

# ==================================================
# 🔄 반복 헤더 제거
# ==================================================
def clean_repeated_headers(text):
    patterns = [
        r"하루에 한 가지씩 성품 바꾸기!.*?\n",
        r"복음안의 성품, 하나님 나라의 성품.*?\n",
        r"구역장 성경공부 공과.*?\n"
    ]
    for p in patterns:
        text = re.sub(p, "", text)
    return text

# ==================================================
# 📑 PDF 페이지 추출
# ==================================================
def extract_pages(pdf_path):
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(len(doc)):
        text = doc[i].get_text()
        text = clean_repeated_headers(text)
        pages.append((i + 1, text))
    return pages

# ==================================================
# 📖 Bible 구절 파싱
# ==================================================
def extract_inline_verses(text):
    pattern = r"\([가-힣]{1,10}\s?\d+:\d+~?\d*\)"
    return re.findall(pattern, text)

# ==================================================
# 🖼 이미지 추출
# ==================================================
def extract_images(pdf_path, filename):
    doc = fitz.open(pdf_path)
    image_data = []

    for page_i in range(len(doc)):
        page = doc[page_i]
        images = page.get_images(full=True)

        for img_i, img in enumerate(images):
            xref = img[0]
            base = doc.extract_image(xref)
            img_bytes = base["image"]

            img_path = os.path.join(
                IMAGE_DIR,
                f"{filename}_p{page_i}_{img_i}.png"
            )

            with open(img_path, "wb") as f:
                f.write(img_bytes)

            image_data.append((img_path, page_i + 1))

    return image_data

# ==================================================
# 🖼 BLIP Caption
# ==================================================
def generate_caption(img_path):
    try:
        image = Image.open(img_path).convert("RGB")
        inputs = processor(image, return_tensors="pt").to(device)

        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=30)

        caption = processor.decode(output[0], skip_special_tokens=True).strip()
        return caption

    except:
        return ""

# ==================================================
# 📦 단일 PDF 인덱싱
# ==================================================
def index_single_pdf(pdf_path, filename, existing_hashes=None):

    file_hash = compute_file_hash(pdf_path)

    if existing_hashes and file_hash in existing_hashes:
        print(f"⏭ 이미 인덱싱됨: {filename}")
        return [], [], [], file_hash

    pages = extract_pages(pdf_path)

    texts = []
    metadata_entries = []
    bm25_tokens = []

    for page_num, text in pages:

        inline_verses = extract_inline_verses(text)

        for verse in inline_verses:
            texts.append(verse)
            bm25_tokens.append(verse.split())

            metadata_entries.append({
                "type": "inline_verse",
                "file": filename,
                "page": page_num,
                "text": verse,
                "file_hash": file_hash
            })

        chunks = chunk_text(text)

        for chunk in chunks:
            texts.append(chunk)
            bm25_tokens.append(chunk.split())

            metadata_entries.append({
                "type": "content",
                "file": filename,
                "page": page_num,
                "text": chunk,
                "file_hash": file_hash
            })

    # 이미지
    image_data = extract_images(pdf_path, filename)

    for img_path, page_num in image_data:
        caption = generate_caption(img_path)

        if caption:
            texts.append(caption)
            bm25_tokens.append(caption.split())

            metadata_entries.append({
                "type": "image_caption",
                "file": filename,
                "page": page_num,
                "image_path": img_path,
                "text": caption,
                "file_hash": file_hash
            })

    if not texts:
        return [], [], [], file_hash

    # 🔥 Batch embedding (GPU 사용)
    vectors = embedder.encode(
        texts,
        normalize_embeddings=True,
        batch_size=32
    )

    return vectors, metadata_entries, bm25_tokens, file_hash

# ==================================================
# 🚀 FAISS 생성
# ==================================================
def create_faiss_index(vectors, use_gpu=True):

    dim = vectors.shape[1]
    cpu_index = faiss.IndexFlatIP(dim)

    if use_gpu and faiss.get_num_gpus() > 0:
        print("🔥 FAISS GPU 사용")
        res = faiss.StandardGpuResources()
        gpu_index = faiss.index_cpu_to_gpu(res, 0, cpu_index)
        gpu_index.add(vectors)
        return faiss.index_gpu_to_cpu(gpu_index)

    print("⚠ FAISS CPU 사용")
    cpu_index.add(vectors)
    return cpu_index

# ==================================================
# 🚀 FULL INDEXING
# ==================================================
def full_indexing():

    print("📚 FULL INDEXING 시작 (GPU 모드)")

    all_vectors = []
    all_metadata = []
    all_tokens = []

    for filename in tqdm(os.listdir(PDF_FOLDER)):
        if not filename.lower().endswith(".pdf"):
            continue

        path = os.path.join(PDF_FOLDER, filename)

        vectors, metadata_entries, bm25_tokens, _ = index_single_pdf(path, filename)

        if len(vectors) == 0:
            continue

        all_vectors.extend(vectors)
        all_metadata.extend(metadata_entries)
        all_tokens.extend(bm25_tokens)

    all_vectors = np.array(all_vectors).astype("float32")

    print("총 벡터 수:", len(all_vectors))

    index = create_faiss_index(all_vectors, use_gpu=True)
    faiss.write_index(index, INDEX_FILE)

    bm25 = BM25Okapi(all_tokens)

    with open(META_FILE, "wb") as f:
        pickle.dump(all_metadata, f)

    with open(BM25_FILE, "wb") as f:
        pickle.dump(bm25, f)

    print("✅ FULL INDEX 완료")

# ==================================================
# 🚀 INCREMENTAL INDEXING
# ==================================================
def incremental_indexing():

    print("⚡ INCREMENTAL INDEXING 시작")

    if not os.path.exists(INDEX_FILE):
        print("기존 인덱스 없음 → FULL 모드 필요")
        return

    index = faiss.read_index(INDEX_FILE)

    with open(META_FILE, "rb") as f:
        metadata = pickle.load(f)

    with open(BM25_FILE, "rb") as f:
        bm25 = pickle.load(f)

    existing_hashes = set(
        m["file_hash"] for m in metadata if "file_hash" in m
    )

    new_vectors = []
    new_metadata = []
    new_tokens = []

    for filename in tqdm(os.listdir(PDF_FOLDER)):
        if not filename.lower().endswith(".pdf"):
            continue

        path = os.path.join(PDF_FOLDER, filename)

        vectors, meta_entries, bm25_tokens, file_hash = index_single_pdf(
            path, filename, existing_hashes
        )

        if len(vectors) == 0:
            continue

        new_vectors.extend(vectors)
        new_metadata.extend(meta_entries)
        new_tokens.extend(bm25_tokens)

    if not new_vectors:
        print("추가할 데이터 없음")
        return

    new_vectors = np.array(new_vectors).astype("float32")

    print("➕ 추가 벡터 수:", len(new_vectors))

    index.add(new_vectors)
    metadata.extend(new_metadata)

    corpus = bm25.corpus + new_tokens
    bm25 = BM25Okapi(corpus)

    faiss.write_index(index, INDEX_FILE)

    with open(META_FILE, "wb") as f:
        pickle.dump(metadata, f)

    with open(BM25_FILE, "wb") as f:
        pickle.dump(bm25, f)

    print("✅ INCREMENTAL 완료")

# ==================================================
# 🎬 실행
# ==================================================
if __name__ == "__main__":

    if MODE == "full":
        full_indexing()

    elif MODE == "incremental":
        incremental_indexing()