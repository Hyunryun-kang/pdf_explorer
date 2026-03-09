# -*- mode: python ; coding: utf-8 -*-
# ============================================================
#  PDF검색기.spec  —  오프라인 PDF 검색기 (LLM 없는 버전)
#  빌드: pyinstaller "PDF검색기.spec"
# ============================================================

import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# ── fitz(PyMuPDF) 바이너리/데이터 수집 ──
fitz_datas, fitz_binaries, fitz_hiddenimports = collect_all("fitz")

a = Analysis(
    ["offline_pdf_searcher_optimized.py"],
    pathex=[],
    binaries=fitz_binaries,
    datas=fitz_datas,
    hiddenimports=fitz_hiddenimports + [
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.font",
        "sqlite3",
        "_sqlite3",
        "concurrent.futures",
        "concurrent.futures.process",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # LLM 관련 — 이 버전에는 불필요
        "llama_cpp",
        "torch",
        "transformers",
        "numpy",          # fitz가 요구하지 않으면 제외
        "scipy",
        "matplotlib",
        "PIL",
        "cv2",
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
    exclude_binaries=True,       # onedir 방식
    name="PDF검색기",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                    # UPX 압축 (없으면 자동 스킵)
    console=False,               # GUI 전용 — 콘솔 창 숨김
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="icon.ico",           # 아이콘 파일이 있으면 주석 해제
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PDF검색기",            # dist/PDF검색기/ 폴더로 출력
)
