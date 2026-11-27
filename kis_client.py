"""
한국투자증권(KIS) 실시간 체결가 웹소켓 클라이언트.

- 서버 시작 시 백그라운드 스레드로 웹소켓 세션을 하나 열어두고 계속 유지.
- 프론트에서 관심종목이 추가되면 ensure_subscribed(code)를 호출해 구독 등록.
- 들어오는 체결가는 QUOTE_CACHE(dict)에 저장, /api/quotes 는 이 캐시를 그대로 읽음.

실전투자 기준 세션 당 구독 가능 종목 수는 최대 40개 내외로 제한되어 있음(정책 변동 가능).
"""

import json
import os
import queue
import threading
import time

import requests
import websocket

APP_KEY = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
IS_VIRTUAL = os.environ.get("KIS_VIRTUAL", "0") == "1"  # 모의투자 계좌면 1

REST_BASE = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
    else "https://openapi.koreainvestment.com:9443"
WS_URL = "ws://ops.koreainvestment.com:31000" if IS_VIRTUAL \
    else "ws://ops.koreainvestment.com:21000"

MAX_SUBSCRIPTIONS = 38  # 안전 마진 (정책상 40 내외)

# 종목코드 -> 최신 시세 dict
QUOTE_CACHE = {}
_cache_lock = threading.Lock()

_subscribed_codes = set()
_pending_queue = queue.Queue()
_ws_ready = threading.Event()
_approval_key = None

# 진단용 상태 - /api/kis-status 에서 그대로 노출
_status = {
    "phase": "not_started",       # not_started -> requesting_approval -> connected -> looping -> error
    "last_error": None,
    "last_error_at": None,
    "connected_at": None,
    "loop_count": 0,              # recv 루프가 몇 번 돌았는지 (스레드 생존 확인용)
    "last_loop_at": None,
}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_status():
    return {**_status, "ws_ready": _ws_ready.is_set(), "subscribed_codes": list(_subscribed_codes)}


_http_session = requests.Session()
_http_session.trust_env = False  # 환경변수의 프록시 설정을 무시 (설정된 프록시가 응답 없으면 무한 대기하는 문제 방지)


def _get_approval_key():
    """웹소켓 접속키(approval_key) 발급"""
    url = f"{REST_BASE}/oauth2/Approval"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": APP_SECRET,
    }
    resp = _http_session.post(url, json=body, timeout=10)
    if resp.status_code != 200:
        print(f"[KIS WS] approval 요청 실패 {resp.status_code}: {resp.text[:300]}", flush=True)
    resp.raise_for_status()
    return resp.json()["approval_key"]


def _subscribe_frame(approval_key, code, tr_type="1"):
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": tr_type,  # 1=등록, 2=해지
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id": "H0STCNT0",  # 실시간 국내주식 체결가
                "tr_key": code,
            }
        },
    })


LAST_RAW_TICK = {}  # code -> raw fields 리스트 (디버그용, 부호 규칙 검증용)


def _parse_tick(raw_fields):
    """H0STCNT0 필드 배열 -> 우리 앱에서 쓰는 dict"""
    # 0:종목코드 1:체결시간 2:현재가 3:전일대비부호 4:전일대비 5:전일대비율
    # 6:가중평균가 7:시가 8:고가 9:저가 10:매도호가1 11:매수호가1
    # 12:체결거래량 13:누적거래량 14:누적거래대금 ...
    code = raw_fields[0]
    LAST_RAW_TICK[code] = raw_fields  # 디버그용 원본 저장
    sign = raw_fields[3]  # 1상한 2상승 3보합 4하한 5하락
    # 주의: raw_fields[4](전일대비), raw_fields[5](전일대비율)는 이미 부호(-)가 포함된 값.
    # sign으로 다시 곱하면 부호가 두 번 적용돼 뒤집힘 (REST와 동일한 실수였음).
    try:
        price = float(raw_fields[2])
        change = float(raw_fields[4])
        rate = float(raw_fields[5])
        open_ = float(raw_fields[7])
        high = float(raw_fields[8])
        low = float(raw_fields[9])
        volume = float(raw_fields[13])
        amount = float(raw_fields[14])  # 누적거래대금(원)
    except (ValueError, IndexError):
        return None

    return {
        "code": code,
        "price": price,
        "change": change,
        "rate": rate,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "sign": sign,
        "ts": raw_fields[1],
        "live": True,
    }


def ensure_subscribed(code):
    """아직 구독 안 한 종목코드면 구독 큐에 넣음 (백그라운드 스레드가 실제 전송)"""
    if code in _subscribed_codes:
        return
    if len(_subscribed_codes) >= MAX_SUBSCRIPTIONS:
        return  # 정책 한도 초과 시 조용히 무시 (필요하면 여기서 에러 표시 가능)
    _pending_queue.put(code)


# 백그라운드 갱신(관심종목 시세·지수)에서 나가는 요청이면 True. 화면에서 누른 요청보다 뒤로 미룸
_bg = threading.local()


def set_background(flag):
    _bg.on = bool(flag)


class _ThrottledSession(requests.Session):
    """KIS REST 호출 속도 조절 + 한도 초과 시 재시도.
    KIS는 초당 호출 수를 넘으면 HTTP 500 + "초당 거래건수를 초과하였습니다"(EGW00201)로 응답함.
    - 모든 요청을 일정 간격으로 보냄. 화면에서 누른 요청(차트·종목 정보 등)이 백그라운드 갱신보다 먼저
    - 한도 초과를 받으면 간격을 늘리고(최대 0.5초), 성공이 이어지면 빠르게 원래 속도로 돌아옴"""

    MAX_INTERVAL = 0.5

    def __init__(self, rps):
        super().__init__()
        self.trust_env = False
        self._base = 1.0 / max(1.0, rps)
        self._interval = self._base
        self._cond = threading.Condition()
        self._next = 0.0
        self._fg_waiting = 0      # 차례를 기다리는 화면 요청 수
        self._slow_logged = False
        self._last_log = 0.0       # 속도 변경 기록은 1분에 한 번만

    def _wait_turn(self):
        fg = not getattr(_bg, "on", False)
        with self._cond:
            if fg:
                self._fg_waiting += 1
            else:
                # 화면 요청이 기다리는 중이면 양보
                while self._fg_waiting > 0:
                    self._cond.wait(timeout=0.2)
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            time.sleep(wait)
        if fg:
            with self._cond:
                self._fg_waiting -= 1
                self._cond.notify_all()

    def _on_limited(self):
        with self._cond:
            self._interval = min(self.MAX_INTERVAL, self._interval * 2)
            self._next = max(self._next, time.monotonic() + 0.3)
            if not self._slow_logged and time.monotonic() - self._last_log > 60:
                self._slow_logged = True
                self._last_log = time.monotonic()
                print(f"[KIS REST] 초당 호출 한도 초과 -> 잠시 호출 간격을 늘려요 ({self._interval:.2f}초)", flush=True)

    def _on_ok(self):
        with self._cond:
            self._interval = max(self._base, self._interval * 0.7)
            if self._slow_logged and self._interval <= self._base:
                self._slow_logged = False   # 되돌린 건 따로 기록하지 않음 (기록이 너무 잦아서)

    @staticmethod
    def _rate_limited(resp):
        if resp.status_code not in (200, 500):
            return False
        t = resp.text[:300]
        return "EGW00201" in t or "초당 거래건수" in t

    def request(self, method, url, *args, **kwargs):
        for attempt in range(5):
            self._wait_turn()
            resp = super().request(method, url, *args, **kwargs)
            if not self._rate_limited(resp):
                self._on_ok()
                break
            self._on_limited()
        if resp.status_code >= 400 and not self._rate_limited(resp):
            # raise_for_status()는 URL만 남기므로 KIS가 보낸 실제 메시지를 함께 기록
            # (한도 초과는 위에서 따로 한 줄로 알리므로 제외)
            path = url.split("?")[0].rsplit("/", 1)[-1]
            print(f"[KIS REST] HTTP {resp.status_code} ({path}): {resp.text[:160]}", flush=True)
        return resp


# 초당 호출 수 (KIS 실전 한도보다 넉넉히 낮게). .env의 KIS_RPS로 조정 가능
try:
    KIS_RPS = float(os.environ.get("KIS_RPS", "10"))
except ValueError:
    KIS_RPS = 10.0
_rest_session = _ThrottledSession(KIS_RPS)

_access_token_lock = threading.Lock()
_access_token = {"token": None, "expires_at": 0}

REST_QUOTE_CACHE = {}          # code -> {"data": {...}, "fetched_at": ts}
REST_QUOTE_TTL = 20            # 초 (종가성 데이터라 자주 바뀌지 않고, 장애 시 반복 실패 방지에도 도움)
REST_TIMEOUT = 5               # 초 (KIS 장애/점검 시 요청 스레드가 오래 안 묶이도록 짧게)


# 토큰을 파일에 보관해서 서버를 재시작해도 재사용 (KIS는 토큰 발급 횟수를 제한함, 대략 1분 1회)
from paths import app_path
TOKEN_FILE = app_path(".kis_token.json")
TOKEN_RETRY_SEC = 65          # 발급 실패 후 이 시간 동안은 다시 요청하지 않음 (제한이 계속 연장되지 않도록)
_token_fail = {"at": 0.0, "msg": None}
_token_file_checked = False


def _load_token_file():
    """저장된 토큰이 같은 앱키로 발급됐고 아직 유효하면 사용"""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("app_key_prefix") == APP_KEY[:8] and d.get("virtual") == IS_VIRTUAL \
                and time.time() < d.get("expires_at", 0) - 60:
            _access_token["token"] = d["token"]
            _access_token["expires_at"] = d["expires_at"]
            left = (d["expires_at"] - time.time()) / 3600
            print(f"[KIS REST] 저장된 access token 재사용 (남은 시간 약 {left:.1f}시간)", flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[KIS REST] 저장된 토큰 읽기 실패(무시하고 새로 발급): {type(e).__name__}: {e}", flush=True)


def _save_token_file():
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": _access_token["token"], "expires_at": _access_token["expires_at"],
                       "app_key_prefix": APP_KEY[:8], "virtual": IS_VIRTUAL}, f)
    except Exception as e:
        print(f"[KIS REST] 토큰 파일 저장 실패(동작에는 지장 없음): {type(e).__name__}: {e}", flush=True)


def _get_access_token():
    """일반 REST 조회용 access token (approval_key와는 별개, 유효기간 24시간)"""
    global _token_file_checked
    with _access_token_lock:
        now = time.time()
        if _access_token["token"] and now < _access_token["expires_at"] - 60:
            return _access_token["token"]

        if not _token_file_checked:
            _token_file_checked = True
            _load_token_file()
            if _access_token["token"] and now < _access_token["expires_at"] - 60:
                return _access_token["token"]

        wait = TOKEN_RETRY_SEC - (now - _token_fail["at"])
        if wait > 0:
            raise RuntimeError(f"접근 토큰 발급 제한으로 {int(wait)}초 후 다시 시도해요 (직전 오류: {_token_fail['msg']})")

        url = f"{REST_BASE}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
        resp = _rest_session.post(url, json=body, timeout=REST_TIMEOUT)
        if resp.status_code != 200:
            _token_fail["at"] = now
            _token_fail["msg"] = f"HTTP {resp.status_code} {resp.text[:200]}"
            print(f"[KIS REST] access token 발급 실패: {_token_fail['msg']}", flush=True)
            raise RuntimeError(f"접근 토큰 발급 실패 ({_token_fail['msg']})")
        data = resp.json()
        _access_token["token"] = data["access_token"]
        _access_token["expires_at"] = now + int(data.get("expires_in", 86400))
        _token_fail["msg"] = None
        _save_token_file()
        print("[KIS REST] access token 발급/갱신 완료 (파일에 저장)", flush=True)
        return _access_token["token"]


def get_rest_quote(code):
    """웹소켓 실시간 틱이 없을 때(장마감 등) 쓰는 REST 시세 조회 - 종가/마지막 체결가를 반환"""
    now = time.time()
    cached = REST_QUOTE_CACHE.get(code)
    if cached and now - cached["fetched_at"] < REST_QUOTE_TTL:
        return cached["data"]

    try:
        token = _get_access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "FHKST01010100",
            "custtype": "P",
        }
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        resp = _rest_session.get(
            f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers, params=params, timeout=REST_TIMEOUT,
        )
        resp.raise_for_status()
        out = resp.json().get("output", {})
        if not out:
            return None

        # 주의: REST 응답의 prdy_vrss/prdy_ctrt는 이미 부호(-)가 포함된 값이라
        # sign 필드로 다시 곱하면 안 됨 (그러면 부호가 두 번 적용돼 뒤집힘).
        # sign은 표시용(상한/하한 등)으로만 참고.
        sign = out.get("prdy_vrss_sign", "3")  # 1상한 2상승 3보합 4하한 5하락

        result = {
            "code": code,
            "price": float(out.get("stck_prpr") or 0),
            "change": float(out.get("prdy_vrss") or 0),
            "rate": float(out.get("prdy_ctrt") or 0),
            "open": float(out.get("stck_oprc") or 0),
            "high": float(out.get("stck_hgpr") or 0),
            "low": float(out.get("stck_lwpr") or 0),
            "volume": float(out.get("acml_vol") or 0),
            "amount": float(out.get("acml_tr_pbmn") or 0),
            "sign": sign,
            "ts": None,
            "live": False,   # 실시간 체결이 아니라 REST 조회(종가 등)임을 표시
        }
        REST_QUOTE_CACHE[code] = {"data": result, "fetched_at": now}
        return result
    except Exception as e:
        print(f"[KIS REST] 시세 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        REST_QUOTE_CACHE[code] = {"data": None, "fetched_at": now}  # 실패도 잠깐 캐싱(연속 실패로 인한 지연 누적 방지)
        return None


# ---------- 종목 정보 (52주 최고/최저, 시총, PER 등) ----------
# 현재가 조회(FHKST01010100) 응답에 함께 들어있는 값들. 장중에도 자주 바뀌지 않아 1분 캐싱.
STOCK_INFO_CACHE = {}
STOCK_INFO_TTL = 60


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def get_stock_info(code):
    now = time.time()
    cached = STOCK_INFO_CACHE.get(code)
    if cached and now - cached["fetched_at"] < STOCK_INFO_TTL:
        return cached["data"]

    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    out = body.get("output") or {}
    if not out:
        raise RuntimeError(body.get("msg1") or "빈 응답")

    data = {
        "sector": (out.get("bstp_kor_isnm") or "").strip() or None,  # 업종명
        "base": _num(out.get("stck_sdpr")),                         # 기준가(전일 종가)
        "w52_high": _num(out.get("w52_hgpr")),
        "w52_high_date": out.get("w52_hgpr_date") or None,
        "w52_low": _num(out.get("w52_lwpr")),
        "w52_low_date": out.get("w52_lwpr_date") or None,
        "amount": _num(out.get("acml_tr_pbmn")),                    # 누적 거래대금(원)
        "market_cap_eok": _num(out.get("hts_avls")),                # 시가총액(억원)
        "foreign_ratio": _num(out.get("hts_frgn_ehrt")),            # 외국인 소진율(%)
        "per": _num(out.get("per")),
        "pbr": _num(out.get("pbr")),
        "eps": _num(out.get("eps")),
        "bps": _num(out.get("bps")),
        "listed_shares": _num(out.get("lstn_stcn")),                # 상장주식수 (보유율 추정용)
    }
    STOCK_INFO_CACHE[code] = {"data": data, "fetched_at": now}
    return data


# ---------- 투자자별 매매동향 (일별 순매수) ----------
INVESTOR_CACHE = {}
INVESTOR_TTL = 300  # 장 마감 후 집계되는 값이라 5분 캐싱


def _fetch_investor_raw(code, market="J"):
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code}
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_daily_price_raw(code):
    """주식현재가 일자별(FHKST01010400): 최근 30거래일, 일별 외국인 소진율(보유율) 포함"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010400",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
              "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _foreign_ratio_by_date(code):
    """날짜 -> 외국인 소진율(%). 실패해도 매매동향 자체는 보여줘야 하므로 빈 dict 반환"""
    try:
        body = _fetch_daily_price_raw(code)
        return {r["stck_bsop_date"]: _num(r.get("hts_frgn_ehrt"))
                for r in (body.get("output") or []) if r.get("stck_bsop_date")}
    except Exception as e:
        print(f"[KIS REST] 일자별 외국인 보유율 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return {}


def _fetch_investor_daily_raw(code, date, market="J"):
    """종목별 투자자매매동향(일별, FHPTJ04160001): date부터 과거로 30거래일 (최신순)"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPTJ04160001",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": market,   # J: KRX, NX: NXT, UN: 통합 (지원 여부 확인용)
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": date,   # 기준일 YYYYMMDD
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    }
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    return resp.status_code, resp.json()


INVESTOR_DAILY_CACHE = {}
# 날짜 지정 매매동향 API는 00:00~15:40에 [OPSQ2001] TIME LIMIT 으로 막힘
TIME_LIMIT_CODE = "OPSQ2001"
TIME_LIMIT_MSG = "이전 매매동향은 KIS 제한으로 15:40 이후에 불러올 수 있어요."


def _time_limited(code, date):
    """제한 시간대: 최근 구간 요청이면 시간 제한 없는 30거래일 API로 대체, 과거 구간 요청이면 안내"""
    if date >= today_kst():
        data = dict(get_investor(code))
        data["limited"] = True   # 프론트: 과거 이어 불러오기를 15:40 이후로 미룸
        return data
    raise RuntimeError(TIME_LIMIT_MSG)


def get_investor_daily(code, date):
    """date(YYYYMMDD) 이전 30거래일의 개인/외국인/기관 순매수 -> [{date, close, individual, foreign, institution, foreign_ratio}] (최신순)
    스크롤로 더 불러올 때는 date를 '가장 오래된 날짜의 전날'로 넘기면 이어짐."""
    today = today_kst()
    key = (code, date)
    ttl = INVESTOR_TTL if date >= today else 3600   # 과거 구간은 바뀌지 않으므로 길게
    now = time.time()
    cached = INVESTOR_DAILY_CACHE.get(key)
    if cached and now - cached["fetched_at"] < ttl:
        return cached["data"]

    # 네이버 등과 같은 KRX+NXT 통합(UN) 기준으로 조회. NXT 미거래 종목 등에서 실패하거나 비면 KRX(J)로 다시 조회
    market = "UN"
    status, body = _fetch_investor_daily_raw(code, date, "UN")
    if body.get("msg_cd") == TIME_LIMIT_CODE:
        return _time_limited(code, date)
    if status != 200 or body.get("rt_cd") != "0" or not body.get("output2"):
        print(f"[KIS REST] 통합(UN) 매매동향 조회 실패/빈 응답 ({code}, {date}) -> KRX(J)로 재조회: "
              f"HTTP {status} [{body.get('msg_cd')}] {body.get('msg1')}", flush=True)
        market = "J"
        status, body = _fetch_investor_daily_raw(code, date, "J")
        if body.get("msg_cd") == TIME_LIMIT_CODE:
            return _time_limited(code, date)
    if status != 200 or body.get("rt_cd") != "0":
        raise RuntimeError(f"KIS 응답 오류 HTTP {status} [{body.get('msg_cd')}] {body.get('msg1')}")

    # 보유율은 일자별 시세 API(최근 30거래일)에서만 나오므로, 최근 구간을 요청할 때만 조회
    recent = (datetime.strptime(date, "%Y%m%d") >= datetime.now(KST).replace(tzinfo=None) - timedelta(days=50))
    ratio = _foreign_ratio_by_date(code) if recent else {}

    rows = []
    for r in body.get("output2") or []:
        d = r.get("stck_bsop_date")
        ind, frg, org = _num(r.get("prsn_ntby_qty")), _num(r.get("frgn_ntby_qty")), _num(r.get("orgn_ntby_qty"))
        if not d or (not ind and not frg and not org):
            continue  # 아직 집계 전인 날(주로 장중의 당일)
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "close": _num(r.get("stck_clpr")),
            "individual": ind, "foreign": frg, "institution": org,
            "foreign_ratio": ratio.get(d),
        })
    rows.sort(key=lambda x: x["date"], reverse=True)
    data = {"rows": rows, "market": market}
    INVESTOR_DAILY_CACHE[key] = {"data": data, "fetched_at": now}
    return data


def get_investor(code):
    """[시간 제한 없는 대체용] 최근 30거래일 개인/외국인/기관 순매수 (FHKST01010900)
    -> {"rows": [...최신순], "market": "UN" 또는 "J"}. 통합(UN)을 먼저 시도하고 안 되면 KRX(J)."""
    now = time.time()
    cached = INVESTOR_CACHE.get(code)
    if cached and now - cached["fetched_at"] < INVESTOR_TTL:
        return cached["data"]

    market = "UN"
    try:
        body = _fetch_investor_raw(code, "UN")
        ok = body.get("rt_cd") == "0" and body.get("output")
    except Exception:
        ok = False
    if not ok:
        market = "J"
        body = _fetch_investor_raw(code, "J")
    if body.get("rt_cd") != "0":
        raise RuntimeError(f"KIS 응답 오류 [{body.get('msg_cd')}] {body.get('msg1')}")
    ratio = _foreign_ratio_by_date(code)
    rows = []
    for r in body.get("output") or []:
        d = r.get("stck_bsop_date")
        ind, frg, org = _num(r.get("prsn_ntby_qty")), _num(r.get("frgn_ntby_qty")), _num(r.get("orgn_ntby_qty"))
        if not d or (not ind and not frg and not org):
            continue  # 아직 집계 전인 날(주로 장중의 당일)은 모두 0이라 제외
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "close": _num(r.get("stck_clpr")),
            "individual": ind, "foreign": frg, "institution": org,
            "foreign_ratio": ratio.get(d),
        })
    rows.sort(key=lambda x: x["date"], reverse=True)
    data = {"rows": rows, "market": market}
    INVESTOR_CACHE[code] = {"data": data, "fetched_at": now}
    return data


# ---------- NXT(넥스트레이드) 시세 ----------
# KRX가 닫혀 있는 시간(프리마켓 08:00~08:50, 애프터마켓 15:30~20:00)에 실제로 거래되는 가격
NXT_CACHE = {}
NXT_TTL = 5


def _fetch_price_raw(code, market):
    """주식현재가 시세(FHKST01010100)를 시장 구분(J/NX/UN)을 바꿔 조회 -> (HTTP 상태, 응답)"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code}
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    return resp.status_code, resp.json()


def get_nxt_quote(code):
    """NXT 현재가 -> {"available": True, "price", "volume", "high", "low"} 또는 {"available": False, "msg"}"""
    now = time.time()
    cached = NXT_CACHE.get(code)
    if cached and now - cached["fetched_at"] < NXT_TTL:
        return cached["data"]
    status, body = _fetch_price_raw(code, "NX")
    out = body.get("output") or {}
    price = _num(out.get("stck_prpr"))
    if status != 200 or body.get("rt_cd") != "0" or not price:
        data = {"available": False, "msg": body.get("msg1")}   # NXT 미거래 종목 등
    else:
        data = {"available": True, "price": price, "volume": _num(out.get("acml_vol")),
                "high": _num(out.get("stck_hgpr")), "low": _num(out.get("stck_lwpr"))}
    NXT_CACHE[code] = {"data": data, "fetched_at": now}
    return data


def _fetch_expected_raw(code, market="J"):
    """주식현재가 호가/예상체결(FHKST01010200) 원본. output2에 동시호가 예상체결 정보"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010200",
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code}
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    return resp.status_code, resp.json()


EXPECTED_CACHE = {}
EXPECTED_TTL = 2


def get_expected(code):
    """동시호가 예상체결 -> {"available", "price", "change", "rate", "volume", "vi"}
    change/rate는 기준가 대비이고 부호 포함 (방향은 antc_cntg_vrss_sign: 1상한 2상승 3보합 4하한 5하락)"""
    now = time.time()
    cached = EXPECTED_CACHE.get(code)
    if cached and now - cached["fetched_at"] < EXPECTED_TTL:
        return cached["data"]
    status, body = _fetch_expected_raw(code, "J")
    o = body.get("output2") or {}
    price = _num(o.get("antc_cnpr"))
    if status != 200 or body.get("rt_cd") != "0" or not price:
        data = {"available": False, "msg": body.get("msg1")}
    else:
        neg = o.get("antc_cntg_vrss_sign") in ("4", "5")
        change = abs(_num(o.get("antc_cntg_vrss")) or 0) * (-1 if neg else 1)
        rate = abs(_num(o.get("antc_cntg_prdy_ctrt")) or 0) * (-1 if neg else 1)
        data = {"available": True, "price": price, "change": change, "rate": rate,
                "volume": _num(o.get("antc_vol")),
                "vi": (o.get("vi_cls_code") or "N") not in ("N", "", "0")}
    EXPECTED_CACHE[code] = {"data": data, "fetched_at": now}
    return data


# ---------- 국내 지수 (코스피 0001, 코스닥 1001) ----------
KR_INDICES = [("kospi", "코스피", "0001"), ("kosdaq", "코스닥", "1001")]
KR_INDEX_CACHE = {"data": None, "fetched_at": 0.0}
KR_INDEX_TTL = 8
KR_CLOSES_CACHE = {}     # code -> {"closes": [...], "fetched_at"}  일별 종가는 자주 안 바뀌므로 1시간


def _kis_get(tr_id, path, params):
    token = _get_access_token()
    headers = {"authorization": f"Bearer {token}", "appkey": APP_KEY, "appsecret": APP_SECRET,
               "tr_id": tr_id, "custtype": "P"}
    resp = _rest_session.get(f"{REST_BASE}{path}", headers=headers, params=params, timeout=REST_TIMEOUT)
    return resp.status_code, resp.json()


def _fetch_kr_index_raw(code):
    """국내업종 현재지수(FHPUP02100000)"""
    return _kis_get("FHPUP02100000", "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                    {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code})


def _fetch_kr_index_daily_raw(code, start, end):
    """국내주식업종기간별시세(FHKUP03500100)"""
    return _kis_get("FHKUP03500100", "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                    {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code,
                     "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})


def _signed(value, sign):
    """KIS 부호 코드(1상한 2상승 3보합 4하한 5하락)를 적용. 값에 이미 부호가 있어도 안전하게"""
    v = _num(value)
    if v is None:
        return None
    return -abs(v) if str(sign) in ("4", "5") else abs(v)


def _kr_closes(code):
    now = time.time()
    c = KR_CLOSES_CACHE.get(code)
    if c and now - c["fetched_at"] < 3600:
        return c["closes"]
    closes = []
    try:
        end = today_kst()
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=25)).strftime("%Y%m%d")
        status, body = _fetch_kr_index_daily_raw(code, start, end)
        rows = body.get("output2") if isinstance(body.get("output2"), list) else []
        rows = [r for r in rows if r.get("stck_bsop_date") and _num(r.get("bstp_nmix_prpr"))]
        rows.sort(key=lambda r: r["stck_bsop_date"])
        closes = [_num(r["bstp_nmix_prpr"]) for r in rows]
    except Exception as e:
        print(f"[KIS REST] 국내 지수 일별 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
    KR_CLOSES_CACHE[code] = {"closes": closes, "fetched_at": now}
    return closes


def get_kr_indices():
    """[{key, name, price, change, rate, closes}]"""
    now = time.time()
    if KR_INDEX_CACHE["data"] and now - KR_INDEX_CACHE["fetched_at"] < KR_INDEX_TTL:
        return KR_INDEX_CACHE["data"]
    result = []
    for key, name, code in KR_INDICES:
        item = {"key": key, "name": name, "price": None, "change": None, "rate": None, "closes": []}
        try:
            status, body = _fetch_kr_index_raw(code)
            o = body.get("output") or {}
            if isinstance(o, list):
                o = o[0] if o else {}
            sign = o.get("prdy_vrss_sign")
            item["price"] = _num(o.get("bstp_nmix_prpr"))
            item["change"] = _signed(o.get("bstp_nmix_prdy_vrss"), sign)
            item["rate"] = _signed(o.get("bstp_nmix_prdy_ctrt"), sign)
            item["detail"] = {
                "open": _num(o.get("bstp_nmix_oprc")), "high": _num(o.get("bstp_nmix_hgpr")), "low": _num(o.get("bstp_nmix_lwpr")),
                "up": _num(o.get("ascn_issu_cnt")), "down": _num(o.get("down_issu_cnt")), "flat": _num(o.get("stnr_issu_cnt")),
                "upper": _num(o.get("uplm_issu_cnt")), "lower": _num(o.get("lslm_issu_cnt")),
                "amount_mil": _num(o.get("acml_tr_pbmn")),            # 거래대금(백만원)
                "year_high": _num(o.get("dryy_bstp_nmix_hgpr")), "year_high_date": o.get("dryy_bstp_nmix_hgpr_date"),
                "year_low": _num(o.get("dryy_bstp_nmix_lwpr")), "year_low_date": o.get("dryy_bstp_nmix_lwpr_date"),
            }
            if not item["price"]:
                print(f"[KIS REST] 국내 지수 응답에 현재가 없음 ({name}): rt_cd={body.get('rt_cd')} {body.get('msg1')}", flush=True)
        except Exception as e:
            print(f"[KIS REST] 국내 지수 조회 실패 ({name}): {type(e).__name__}: {e}", flush=True)
        item["closes"] = _kr_closes(code)
        result.append(item)
        time.sleep(0.06)
    if any(i["price"] for i in result):
        KR_INDEX_CACHE.update(data=result, fetched_at=now)
    return result


# ---------- 지수 차트 (기간별 시세) ----------
INDEX_HISTORY_CACHE = {}


def get_index_history(key, start, end, period="D"):
    """지수 OHLC -> {"name", "bars": [{time, open, high, low, close, volume}]} (오래된 순)
    국내(코스피/코스닥)는 FHKUP03500100, 미국은 FHKST03030100. 한 번에 받는 건수가 제한돼 있어 종료일을 옮겨가며 반복"""
    kr = {k: (n, c) for k, n, c in KR_INDICES}
    us = {k: (n, c) for k, n, c in US_INDICES}
    if key not in kr and key not in us:
        raise ValueError("지원하지 않는 지수예요.")
    ck = (key, start, end, period)
    ttl = 60 if end >= today_kst() else 3600
    now = time.time()
    cached = INDEX_HISTORY_CACHE.get(ck)
    if cached and now - cached["fetched_at"] < ttl:
        return cached["data"]

    name, code = kr.get(key) or us.get(key)
    pre = "bstp_nmix_" if key in kr else "ovrs_nmix_"
    bars, cur_end = {}, end
    for _ in range(15):
        if key in kr:
            status, body = _kis_get("FHKUP03500100", "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                                    {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": start,
                                     "FID_INPUT_DATE_2": cur_end, "FID_PERIOD_DIV_CODE": period})
        else:
            status, body = _fetch_overseas_index_raw(code, start, cur_end, period)
        if body.get("rt_cd") != "0":
            if bars:
                break
            raise RuntimeError(f"KIS 응답 오류 [{body.get('msg_cd')}] {body.get('msg1')}")
        rows = [r for r in (body.get("output2") or []) if r.get("stck_bsop_date") and _num(r.get(pre + "prpr"))]
        if not rows:
            break
        for r in rows:
            d, close = r["stck_bsop_date"], _num(r[pre + "prpr"])
            bars[d] = {"time": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                       "open": _num(r.get(pre + "oprc")) or close, "high": _num(r.get(pre + "hgpr")) or close,
                       "low": _num(r.get(pre + "lwpr")) or close, "close": close,
                       "volume": _num(r.get("acml_vol")) or 0}
        oldest = min(r["stck_bsop_date"] for r in rows)
        if oldest <= start:
            break
        nxt = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        if nxt >= cur_end:
            break
        cur_end = nxt
        time.sleep(0.06)
    data = {"name": name, "bars": [bars[d] for d in sorted(bars) if d >= start]}
    INDEX_HISTORY_CACHE[ck] = {"data": data, "fetched_at": now}
    return data


# ---------- 미국 지수 ----------
# 확인 결과: SPX/COMP/SOX는 output1에 현재가 요약이 오고, .DJI는 output1이 0이라 output2(일별)로 계산
US_INDICES = [("dji", "다우", ".DJI"), ("spx", "S&P500", "SPX"), ("comp", "나스닥", "COMP"), ("sox", "필라델피아 반도체", "SOX")]
US_INDEX_CACHE = {"data": None, "fetched_at": 0.0}
US_INDEX_TTL = 60


def get_us_indices():
    """[{key, name, price, change, rate, date, closes}] - closes는 최근 일별 종가(오래된 순, 스파크라인용)"""
    now = time.time()
    if US_INDEX_CACHE["data"] and now - US_INDEX_CACHE["fetched_at"] < US_INDEX_TTL:
        return US_INDEX_CACHE["data"]
    end = today_kst()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=25)).strftime("%Y%m%d")
    prev = {i["key"]: i for i in (US_INDEX_CACHE["data"] or [])}
    result = []
    for key, name, code in US_INDICES:
        item = {"key": key, "name": name, "price": None, "change": None, "rate": None, "date": None, "closes": [], "detail": {}}
        try:
            status, body = _fetch_overseas_index_raw(code, start, end)
            if body.get("rt_cd") != "0":   # 초당 호출 제한 등 일시 실패면 잠깐 뒤 한 번 더
                time.sleep(0.4)
                status, body = _fetch_overseas_index_raw(code, start, end)
            rows = body.get("output2") if isinstance(body.get("output2"), list) else []
            rows = [r for r in rows if _num(r.get("ovrs_nmix_prpr"))]
            rows.sort(key=lambda r: r.get("stck_bsop_date", ""))
            item["closes"] = [_num(r["ovrs_nmix_prpr"]) for r in rows]
            item["date"] = rows[-1]["stck_bsop_date"] if rows else None
            o1 = body.get("output1") or {}
            if _num(o1.get("ovrs_nmix_prpr")):
                item["price"] = _num(o1.get("ovrs_nmix_prpr"))
                item["change"] = _num(o1.get("ovrs_nmix_prdy_vrss"))
                item["rate"] = _num(o1.get("prdy_ctrt"))
            elif len(rows) >= 2:   # 요약이 비어 있으면(다우) 최근 두 날짜 종가로 계산
                cur, before = item["closes"][-1], item["closes"][-2]
                item["price"], item["change"] = cur, round(cur - before, 2)
                item["rate"] = round((cur - before) / before * 100, 2)
            if rows:
                last = rows[-1]
                item["detail"] = {"open": _num(last.get("ovrs_nmix_oprc")), "high": _num(last.get("ovrs_nmix_hgpr")),
                                  "low": _num(last.get("ovrs_nmix_lwpr"))}
        except Exception as e:
            print(f"[KIS REST] 미국 지수 조회 실패 ({name} {code}): {type(e).__name__}: {e}", flush=True)
        if item["price"] is None and prev.get(key, {}).get("price") is not None:
            item = dict(prev[key])   # 이번에 실패하면 직전에 받은 값 유지
        result.append(item)
        time.sleep(0.06)
    if any(i["price"] for i in result):
        US_INDEX_CACHE.update(data=result, fetched_at=now)
    return result


# ---------- 해외 지수 원본 조회 ----------
def _fetch_overseas_index_raw(code, start, end, period="D"):
    """해외주식 종목/지수/환율 기간별시세(FHKST03030100). 시장 구분 N = 해외지수"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03030100",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "N",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": period,
    }
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    return resp.status_code, resp.json()


STOCK_NAME_CACHE = {}  # code -> name (검증된 종목이라 자주 안 바뀌므로 영구 캐싱)


def get_stock_name(code):
    """내장 리스트에 없는 종목코드의 이름을 KIS 상품기본조회 API로 조회"""
    if code in STOCK_NAME_CACHE:
        return STOCK_NAME_CACHE[code]
    try:
        token = _get_access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "CTPF1002R",
            "custtype": "P",
        }
        params = {"PDNO": code, "PRDT_TYPE_CD": "300"}
        r = _rest_session.get(
            f"{REST_BASE}/uapi/domestic-stock/v1/quotations/search-info",
            headers=headers, params=params, timeout=REST_TIMEOUT,
        )
        r.raise_for_status()
        name = r.json().get("output", {}).get("prdt_abrv_name")
        if name:
            name = name.strip()
            STOCK_NAME_CACHE[code] = name
        return name
    except Exception as e:
        print(f"[KIS REST] 종목명 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return None


# ---------- 기간별 시세 (일/주/월/년봉) ----------
# 국내주식기간별시세 API: 한 번 호출에 최대 100건까지만 내려오므로,
# 요청 구간이 길면 가장 오래된 날짜 전날을 종료일로 바꿔가며 반복 조회함.

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

HISTORY_CACHE = {}          # (code, start, end, period, adj) -> {"data": {...}, "fetched_at": ts}
HISTORY_TTL_PAST = 3600     # 오늘이 포함 안 된 과거 구간은 바뀌지 않으므로 길게 캐싱
HISTORY_TTL_TODAY = 30      # 오늘이 포함된 구간은 짧게
HISTORY_MAX_PAGES = 10      # 100건 x 10 = 일봉 약 4년치가 상한 (그 이상은 주봉/월봉 권장)


def today_kst():
    return datetime.now(KST).strftime("%Y%m%d")


def _fetch_chart_page(code, start, end, period, adj):
    """기간별시세 1페이지(최대 100건) 원본 응답"""
    token = _get_access_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start,      # 조회 시작일 YYYYMMDD
        "FID_INPUT_DATE_2": end,        # 조회 종료일 YYYYMMDD
        "FID_PERIOD_DIV_CODE": period,  # D 일 / W 주 / M 월 / Y 년
        "FID_ORG_ADJ_PRC": adj,         # 0 수정주가 / 1 원주가
    }
    resp = _rest_session.get(
        f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers=headers, params=params, timeout=REST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_history(code, start, end, period="D", adj="0"):
    """기간별 OHLCV를 오래된 순으로 반환 -> {"name", "bars": [{time, open, high, low, close, volume}], "pages"}
    실패 시 예외를 그대로 올림 (호출부에서 에러 메시지로 변환)."""
    key = (code, start, end, period, adj)
    now = time.time()
    ttl = HISTORY_TTL_TODAY if end >= today_kst() else HISTORY_TTL_PAST
    cached = HISTORY_CACHE.get(key)
    if cached and now - cached["fetched_at"] < ttl:
        return cached["data"]

    bars = {}
    name = None
    cur_end = end
    pages = 0
    for _ in range(HISTORY_MAX_PAGES):
        data = _fetch_chart_page(code, start, cur_end, period, adj)
        pages += 1
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"KIS 응답 오류 [{data.get('msg_cd')}] {data.get('msg1')}")

        name = name or ((data.get("output1") or {}).get("hts_kor_isnm") or "").strip() or None
        # 데이터가 없는 날은 빈 문자열 행으로 오는 경우가 있어 날짜/종가 있는 행만 사용
        rows = [r for r in (data.get("output2") or [])
                if r.get("stck_bsop_date") and r.get("stck_clpr")]
        if not rows:
            break

        for r in rows:
            d = r["stck_bsop_date"]
            close = float(r.get("stck_clpr") or 0)
            if close <= 0:
                continue
            bars[d] = {
                "time": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "open": float(r.get("stck_oprc") or close),
                "high": float(r.get("stck_hgpr") or close),
                "low": float(r.get("stck_lwpr") or close),
                "close": close,
                "volume": float(r.get("acml_vol") or 0),
                "amount": float(r.get("acml_tr_pbmn") or 0),  # 거래대금(원)
            }

        oldest = min(r["stck_bsop_date"] for r in rows)
        if oldest <= start:
            break
        next_end = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        if next_end >= cur_end:  # 진행이 안 되면 무한루프 방지
            break
        cur_end = next_end
        time.sleep(0.06)  # REST 초당 호출 제한 여유

    result = {
        "name": name,
        "bars": [bars[d] for d in sorted(bars)],
        "pages": pages,
    }
    HISTORY_CACHE[key] = {"data": result, "fetched_at": now}
    return result


def get_quote(code):
    with _cache_lock:
        return QUOTE_CACHE.get(code)


def _run_forever():
    global _approval_key

    print("[KIS WS] 백그라운드 스레드 시작", flush=True)
    fails = 0   # 연속 실패 횟수 (연결 성공하면 0으로)

    while True:
        try:
            _status["phase"] = "requesting_approval"
            print("[KIS WS] approval_key 요청 중...", flush=True)
            _approval_key = _get_approval_key()
            print(f"[KIS WS] approval_key 발급 성공: {_approval_key[:8]}...", flush=True)

            _status["phase"] = "connecting"
            ws = websocket.WebSocket()
            ws.settimeout(10)  # 연결 자체가 막혀있을 때 무한 대기 방지
            ws.connect(WS_URL, ping_interval=60)
            print(f"[KIS WS] 웹소켓 연결 성공: {WS_URL}" + (f" (재시도 {fails}번 만에)" if fails else ""), flush=True)
            fails = 0
            ws.settimeout(1.0)
            _subscribed_codes.clear()
            _ws_ready.set()
            _status["phase"] = "looping"
            _status["connected_at"] = _now()

            while True:
                _status["loop_count"] += 1
                _status["last_loop_at"] = _now()
                # 큐에 쌓인 신규 구독 요청 처리
                while not _pending_queue.empty():
                    code = _pending_queue.get()
                    if code in _subscribed_codes:
                        continue
                    ws.send(_subscribe_frame(_approval_key, code))
                    _subscribed_codes.add(code)
                    print(f"[KIS WS] 구독 등록: {code}", flush=True)
                    time.sleep(0.05)  # 과도한 연속 전송 방지

                try:
                    data = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue

                if not data:
                    continue

                if data[0] in ("0", "1"):
                    parts = data.split("|")
                    if len(parts) < 4:
                        continue
                    tr_id = parts[1]
                    if tr_id != "H0STCNT0":
                        continue
                    # 여러 건이 한 번에 올 수 있음 (^로 필드 구분, 종목 단위 반복)
                    body = parts[3]
                    fields = body.split("^")
                    tick = _parse_tick(fields)
                    if tick:
                        with _cache_lock:
                            QUOTE_CACHE[tick["code"]] = tick
                else:
                    # PINGPONG 등 제어 메시지
                    try:
                        msg = json.loads(data)
                        if msg.get("header", {}).get("tr_id") == "PINGPONG":
                            ws.send(data)  # 그대로 되돌려줘야 연결 유지됨
                    except json.JSONDecodeError:
                        pass

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            fails += 1
            # 재시작 직후 등 일시적인 끊김이 대부분이라 평소엔 한 줄만.
            # 5번 연속 실패하면 원인 확인용으로 상세 내역을 한 번 출력
            print(f"[KIS WS] 연결 실패({fails}번째), 5초 후 재시도: {type(e).__name__}: {e}", flush=True)
            if fails == 5:
                print("[KIS WS] 5번 연속 실패해서 상세 내역을 남겨요:\n" + tb, flush=True)
            _status["last_traceback"] = tb
            _status["phase"] = "error"
            _status["last_error"] = f"{type(e).__name__}: {e}"
            _status["last_error_at"] = _now()
            _ws_ready.clear()
            time.sleep(5)


def restart():
    """재배포 없이 세션/스레드를 강제로 새로 시작 (디버깅용)"""
    global _http_session
    print("[KIS WS] 강제 재시작 요청됨 - 세션 새로 생성", flush=True)
    _http_session = requests.Session()
    _http_session.trust_env = False
    _status["phase"] = "restarting"
    _status["last_error"] = None
    _status["last_error_at"] = None
    t = threading.Thread(target=_run_forever, daemon=True)
    t.start()


def start_background():
    if not APP_KEY or not APP_SECRET:
        _status["phase"] = "disabled_no_key"
        print("[KIS WS] KIS_APP_KEY / KIS_APP_SECRET 환경변수가 설정되지 않았습니다. "
              "실시간 시세가 동작하지 않습니다.", flush=True)
        return
    print(f"[KIS WS] APP_KEY 확인됨 ({APP_KEY[:4]}...), 백그라운드 스레드 생성", flush=True)
    t = threading.Thread(target=_run_forever, daemon=True)
    t.start()
