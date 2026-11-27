"""
전체 상장종목 리스트 (한국투자증권 공식 종목 마스터 파일).

- 코스피/코스닥 마스터 zip을 받아서 [{name, code, market}]로 변환
- stock_master.json에 저장해두고 하루 한 번만 새로 받음 (오프라인이면 저장본 사용)
- 다운로드와 저장본이 모두 없으면 None을 반환 -> 호출부에서 stock_list.json으로 대체

마스터 파일 형식 (KIS 공식 샘플 코드 기준, cp949 고정폭):
  각 줄 = [단축코드 9][표준코드 12][한글명 ...][뒤쪽 고정폭 속성 N바이트]
  뒤쪽 고정폭 길이: 코스피 228, 코스닥 222
"""

import io
import json
import os
import time
import zipfile

import requests

MASTER_URLS = {
    "KOSPI": ("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", "kospi_code.mst", 228),
    "KOSDAQ": ("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", "kosdaq_code.mst", 222),
}
from paths import app_path
CACHE_FILE = app_path("stock_master.json")
MAX_AGE_SEC = 24 * 3600

_session = requests.Session()
_session.trust_env = False

last_info = {"source": None, "count": 0, "error": None, "updated_at": None}


def _parse(raw_bytes, market, tail_len):
    out = []
    for line in raw_bytes.decode("cp949", errors="replace").splitlines():
        if len(line) <= tail_len + 21:
            continue
        head = line[: len(line) - tail_len]
        code = head[0:9].strip()
        name = head[21:].strip()
        if len(code) == 6 and name:
            out.append({"name": name, "code": code, "market": market})
    return out


def _download():
    stocks = []
    for market, (url, member, tail_len) in MASTER_URLS.items():
        r = _session.get(url, timeout=15)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            stocks += _parse(z.read(member), market, tail_len)
    return stocks


def load():
    """전체 종목 리스트 반환. 실패하면 None."""
    # 1) 하루 이내 저장본이 있으면 그대로 사용
    if os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < MAX_AGE_SEC:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            last_info.update(source="cache", count=len(data), error=None,
                             updated_at=time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(CACHE_FILE))))
            return data
        except Exception:
            pass

    # 2) 새로 다운로드
    try:
        data = _download()
        if len(data) < 1000:  # 파싱이 어긋났으면 이상하게 적게 나옴 -> 저장하지 않음
            raise RuntimeError(f"파싱 결과가 {len(data)}건뿐이라 형식이 바뀐 것 같음")
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        last_info.update(source="download", count=len(data), error=None, updated_at=time.strftime("%Y-%m-%d %H:%M"))
        print(f"[MASTER] 종목 마스터 다운로드 완료: {len(data)}건", flush=True)
        return data
    except Exception as e:
        last_info["error"] = f"{type(e).__name__}: {e}"
        print(f"[MASTER] 종목 마스터 다운로드 실패: {last_info['error']}", flush=True)

    # 3) 오래된 저장본이라도 있으면 사용
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            last_info.update(source="stale_cache", count=len(data))
            return data
        except Exception:
            pass
    return None
