# PDF 검색기 — 빌드 & 배포 가이드

## 파일 구성

```
프로젝트 폴더/
├── offline_pdf_searcher_optimized.py       ← 기본 버전 소스
├── offline_pdf_search_sum_llm_optimized.py ← LLM 버전 소스
├── PDF검색기.spec                           ← 기본 버전 빌드 설정
├── PDF검색기_LLM.spec                       ← LLM 버전 빌드 설정
├── build.bat                               ← 빌드 실행 스크립트
└── BUILD_README.md                         ← 이 파일
```

---

## 1. 사전 준비

```bash
pip install pyinstaller
pip install pymupdf
pip install llama-cpp-python   # LLM 버전만 필요
```

---

## 2. 빌드 실행

### 방법 A — 배치 스크립트 사용 (권장)
```
build.bat 더블클릭 → 버전 선택 (1/2/3)
```

### 방법 B — 직접 명령어
```bash
# 기본 버전
pyinstaller "PDF검색기.spec" --clean --noconfirm

# LLM 버전
pyinstaller "PDF검색기_LLM.spec" --clean --noconfirm
```

---

## 3. 빌드 결과물

```
dist/
├── PDF검색기/
│   ├── PDF검색기.exe       ← 실행 파일
│   ├── settings.json       ← 뷰어 설정 (자동 생성)
│   ├── synonyms.json       ← 동의어 사전 (자동 생성)
│   └── (기타 dll, 라이브러리)
│
└── PDF검색기_LLM/
    ├── PDF검색기_LLM.exe
    ├── settings.json
    ├── synonyms.json
    ├── models/             ← .gguf 모델 파일을 여기에 넣기
    └── (기타 dll, 라이브러리)
```

---

## 4. LLM 모델 파일 준비

`.gguf` 파일을 별도로 다운로드해서 `models/` 폴더에 넣어두세요.

| 모델 | 크기 | 다운로드 |
|---|---|---|
| Qwen2.5-3B-Instruct-Q4_K_M.gguf | ~2GB | huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF |
| Qwen2.5-7B-Instruct-Q4_K_M.gguf | ~4.5GB | huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF |
| EXAONE-3.5-2.4B-Instruct-Q4_K_M.gguf | ~1.7GB | huggingface.co/LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct-GGUF |

앱 실행 후 **뷰어/LLM 설정** → **LLM 모델(.gguf) 경로** 에서 파일을 선택합니다.

---

## 5. 배포 시 주의사항

- `dist/PDF검색기/` 폴더 **전체**를 배포해야 합니다 (exe 단독 배포 불가)
- `offline_pdf_index.db` (인덱스 DB)는 실행 시 자동 생성됩니다
- 모델 파일(.gguf)은 용량이 크므로 **별도 링크로 제공** 권장

---

## 6. 빌드 오류 대처

### `ModuleNotFoundError: fitz`
```bash
pip install --upgrade pymupdf
pyinstaller "PDF검색기.spec" --clean --noconfirm
```

### `llama_cpp` 관련 DLL 오류
```bash
pip install --upgrade llama-cpp-python
```

### 한글 경로 문제
소스 파일과 `.spec` 파일을 영문 경로에 두고 빌드하세요.
