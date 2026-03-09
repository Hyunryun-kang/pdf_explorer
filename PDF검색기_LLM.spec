# -*- mode: python ; coding: utf-8 -*-
# ============================================================
#  PDF검색기_LLM.spec  —  오프라인 PDF 검색기 + 로컬 LLM 요약 버전
#  빌드: pyinstaller "PDF검색기_LLM.spec"
#
#  주의:
#   - llama-cpp-python 의 .dll/.so 가 크기 때문에 onedir 방식 사용
#   - .gguf 모델 파일은 exe와 별도 배포 (dist/PDF검색기_LLM/models/ 권장)
# ============================================================

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# ── fitz(PyMuPDF) 수집 ──
fitz_datas, fitz_binaries, fitz_hiddenimports = collect_all("fitz")

# ── llama_cpp 수집 ──
llama_datas, llama_binaries, llama_hiddenimports = collect_all("llama_cpp")

a = Analysis(
    ["offline_pdf_search_sum_llm_optimized.py"],
    pathex=[],
    binaries=fitz_binaries + llama_binaries,
    datas=fitz_datas + llama_datas,
    hiddenimports=fitz_hiddenimports + llama_hiddenimports + [
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.font",
        "sqlite3",
        "_sqlite3",
        "concurrent.futures",
        "concurrent.futures.process",
        "ctypes",
        "ctypes.util",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "transformers",
        "scipy",
        "matplotlib",
        "PIL",
        "cv2",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF검색기_LLM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,               # GUI 전용
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # llama_cpp 핵심 바이너리는 UPX 압축 제외 (압축 시 오동작 가능)
        "llama.dll",
        "libllama.so",
        "llama_cpp*.dll",
        "ggml*.dll",
    ],
    name="PDF검색기_LLM",
)
