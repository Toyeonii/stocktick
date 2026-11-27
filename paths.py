"""
실행 위치 기준 경로.

- 개발 중(python app.py): 둘 다 소스 폴더
- 실행 파일(StockTick.exe):
    APP_DIR = exe가 있는 폴더  -> .env, 토큰, 관심종목, 종목 목록 캐시, 웨일 프로필 (계속 남아야 하는 파일)
    RES_DIR = exe 안에 묶여 있다가 실행 시 임시 폴더에 풀린 파일 -> index.html, static, stock_list.json 등 (읽기 전용)
"""
import os
import sys

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)


def app_path(*parts):
    """보존돼야 하는 설정·데이터 파일 경로"""
    return os.path.join(APP_DIR, *parts)


def res_path(*parts):
    """함께 묶인 읽기 전용 파일 경로"""
    return os.path.join(RES_DIR, *parts)
