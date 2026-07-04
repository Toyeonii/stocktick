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


def _parse_tick(raw_fields):
    """H0STCNT0 필드 배열 -> 우리 앱에서 쓰는 dict"""
    # 0:종목코드 1:체결시간 2:현재가 3:전일대비부호 4:전일대비 5:전일대비율
    # 6:가중평균가 7:시가 8:고가 9:저가 10:매도호가1 11:매수호가1
    # 12:체결거래량 13:누적거래량 14:누적거래대금 ...
    code = raw_fields[0]
    sign = raw_fields[3]  # 1상한 2상승 3보합 4하한 5하락
    mult = -1 if sign in ("4", "5") else 1
    try:
        price = float(raw_fields[2])
        change = float(raw_fields[4]) * mult
        rate = float(raw_fields[5]) * mult
        open_ = float(raw_fields[7])
        high = float(raw_fields[8])
        low = float(raw_fields[9])
        volume = float(raw_fields[13])
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


_access_token_lock = threading.Lock()
_access_token = {"token": None, "expires_at": 0}

REST_QUOTE_CACHE = {}          # code -> {"data": {...}, "fetched_at": ts}
REST_QUOTE_TTL = 5             # 초 (너무 자주 REST 호출하지 않도록 짧게 캐싱)


def _get_access_token():
    """일반 REST 조회용 access token 발급 (approval_key와는 별개, 유효기간 24시간이라 캐싱해서 재사용)"""
    with _access_token_lock:
        now = time.time()
        if _access_token["token"] and now < _access_token["expires_at"] - 60:
            return _access_token["token"]

        url = f"{REST_BASE}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
        resp = _http_session.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _access_token["token"] = data["access_token"]
        _access_token["expires_at"] = now + int(data.get("expires_in", 86400))
        print("[KIS REST] access token 발급/갱신 완료", flush=True)
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
        resp = _http_session.get(
            f"{REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers, params=params, timeout=8,
        )
        resp.raise_for_status()
        out = resp.json().get("output", {})
        if not out:
            return None

        sign = out.get("prdy_vrss_sign", "3")  # 1상한 2상승 3보합 4하한 5하락
        mult = -1 if sign in ("4", "5") else 1

        result = {
            "code": code,
            "price": float(out.get("stck_prpr") or 0),
            "change": float(out.get("prdy_vrss") or 0) * mult,
            "rate": float(out.get("prdy_ctrt") or 0) * mult,
            "open": float(out.get("stck_oprc") or 0),
            "high": float(out.get("stck_hgpr") or 0),
            "low": float(out.get("stck_lwpr") or 0),
            "volume": float(out.get("acml_vol") or 0),
            "sign": sign,
            "ts": None,
            "live": False,   # 실시간 체결이 아니라 REST 조회(종가 등)임을 표시
        }
        REST_QUOTE_CACHE[code] = {"data": result, "fetched_at": now}
        return result
    except Exception as e:
        print(f"[KIS REST] 시세 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return None


def get_quote(code):
    with _cache_lock:
        return QUOTE_CACHE.get(code)


def _run_forever():
    global _approval_key

    print("[KIS WS] 백그라운드 스레드 시작", flush=True)

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
            print(f"[KIS WS] 웹소켓 연결 성공: {WS_URL}", flush=True)
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
            print(f"[KIS WS] 연결 오류, 5초 후 재시도: {e}", flush=True)
            print(tb, flush=True)
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
