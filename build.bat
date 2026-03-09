@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion

echo ============================================================
echo  PDF 검색기 빌드 스크립트
echo  PyInstaller 를 이용해 exe 를 생성합니다.
echo ============================================================
echo.

:: ── 빌드할 버전 선택 ──
echo 빌드할 버전을 선택하세요:
echo   [1] PDF검색기       (LLM 없는 기본 버전)
echo   [2] PDF검색기_LLM   (로컬 LLM 요약 버전)
echo   [3] 둘 다 빌드
echo.
set /p CHOICE="선택 (1/2/3): "

:: ── PyInstaller 설치 확인 ──
python -m pyinstaller --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo [오류] PyInstaller 가 설치되어 있지 않습니다.
    echo        설치 명령: pip install pyinstaller
    pause
    exit /b 1
)

:: ── 빌드 함수 정의 ──
goto BUILD_START

:BUILD_PLAIN
echo.
echo ── PDF검색기 (기본 버전) 빌드 시작 ──
echo.
python -m pyinstaller "PDF검색기.spec" --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [실패] PDF검색기 빌드 중 오류가 발생했습니다.
    echo        위 로그를 확인하세요.
) else (
    echo.
    echo [완료] dist\PDF검색기\PDF검색기.exe 생성됨
    :: settings.json, synonyms.json 복사
    if exist settings.json copy settings.json dist\PDF검색기\ > nul
    if exist synonyms.json copy synonyms.json dist\PDF검색기\ > nul
    echo        설정 파일 복사 완료
)
goto :EOF

:BUILD_LLM
echo.
echo ── PDF검색기_LLM (LLM 버전) 빌드 시작 ──
echo.
python -m pyinstaller "PDF검색기_LLM.spec" --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [실패] PDF검색기_LLM 빌드 중 오류가 발생했습니다.
    echo        위 로그를 확인하세요.
) else (
    echo.
    echo [완료] dist\PDF검색기_LLM\PDF검색기_LLM.exe 생성됨
    :: 모델 폴더 생성
    if not exist dist\PDF검색기_LLM\models mkdir dist\PDF검색기_LLM\models
    :: 설정 파일 복사
    if exist settings.json copy settings.json dist\PDF검색기_LLM\ > nul
    if exist synonyms.json copy synonyms.json dist\PDF검색기_LLM\ > nul
    echo        설정 파일 복사 완료
    echo.
    echo [안내] .gguf 모델 파일을 dist\PDF검색기_LLM\models\ 에 넣어두면
    echo        앱 실행 후 설정에서 쉽게 찾을 수 있습니다.
)
goto :EOF

:: ── 메인 분기 ──
:BUILD_START
if "%CHOICE%"=="1" (
    call :BUILD_PLAIN
) else if "%CHOICE%"=="2" (
    call :BUILD_LLM
) else if "%CHOICE%"=="3" (
    call :BUILD_PLAIN
    call :BUILD_LLM
) else (
    echo 잘못된 선택입니다.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  빌드 완료. dist\ 폴더를 확인하세요.
echo ============================================================
echo.
pause
