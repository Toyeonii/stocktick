import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from paths import FROZEN, app_path, res_path

# 콘솔 창 없이 만든 실행 파일은 print가 갈 곳이 없으므로 exe 옆 stocktick.log 에 기록
if FROZEN and (sys.stdout is None or sys.stderr is None):
    _log_path = app_path("stocktick.log")
    try:
        if os.path.exists(_log_path) and os.path.getsize(_log_path) > 5 * 1024 * 1024:
            os.remove(_log_path)   # 5MB 넘으면 새로 시작
    except OSError:
        pass
    _log = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} StockTick 시작 =====", flush=True)

# .env는 포트 설정에도 쓰이므로 가장 먼저 읽음
from dotenv import load_dotenv
load_dotenv(app_path(".env"))  # exe(또는 app.py) 옆의 .env에서 KIS 키, 포트 등 로드


def _port_from_env():
    """STOCKTICK_PORT로 포트 변경 (맥은 AirPlay 수신 모드가 5000번을 씀)"""
    raw = os.environ.get("STOCKTICK_PORT", "").strip()
    if not raw:
        return 5000
    if raw.isdigit() and 1024 <= int(raw) <= 65535:
        return int(raw)
    print(f"STOCKTICK_PORT 값({raw})이 올바르지 않아 5000번을 씁니다. 1024~65535 사이 숫자로 지정해 주세요.", flush=True)
    return 5000


PORT = _port_from_env()
# 실행 파일: 앱 창에서 요청이 이 시간(초) 동안 없으면 서버 종료 (= 앱 창을 닫으면 잠시 뒤 꺼짐)
IDLE_EXIT_SEC = int(os.environ.get("STOCKTICK_IDLE_EXIT_SEC", "180"))
APP_URL = f"http://localhost:{PORT}"


def open_app_window():
    """실행 파일로 켰을 때 앱 창 띄우기 (배치 파일이 하던 일).
    웨일 -> 엣지 순서로 --app 창을 띄우고, 둘 다 없으면 기본 브라우저로 엶"""
    size = os.environ.get("STOCKTICK_WINDOW_SIZE", "1536,1112")
    candidates = [
        os.environ.get("STOCKTICK_BROWSER", ""),
        r"C:\Program Files\Naver\Naver Whale\Application\whale.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Naver\Naver Whale\Application\whale.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={APP_URL}", f"--window-size={size}",
                              f"--user-data-dir={app_path('whale-profile')}"])
            return
    webbrowser.open(APP_URL)


_last_request = time.time()


def _idle_watchdog():
    """앱 창이 열려 있는 동안은 2초마다(최소화해도 1분에 한 번 이상) 요청이 옴.
    IDLE_EXIT_SEC 동안 아무 요청이 없으면 앱 창이 닫힌 것으로 보고 서버 종료"""
    while True:
        time.sleep(10)
        idle = time.time() - _last_request
        if idle > IDLE_EXIT_SEC:
            print(f"앱 창에서 {int(idle)}초 동안 요청이 없어 서버를 종료해요.", flush=True)
            os._exit(0)


def _open_when_ready():
    """서버가 응답하기 시작하면 앱 창을 띄움 (최대 20초 대기)"""
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    open_app_window()


def _exit_if_already_running(port):
    """이미 서버가 떠 있으면 아무것도 시작하지 않고 종료 (KIS 웹소켓 중복 접속 방지).
    실시간 접속·종목 목록 다운로드보다 먼저 실행돼야 하므로 파일 맨 위에서 호출."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return  # 포트가 비어 있음 -> 정상 시작
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/kis-status", timeout=3) as r:
            is_stocktick = "phase" in json.loads(r.read().decode("utf-8"))
    except Exception:
        is_stocktick = False
    if is_stocktick:
        print(f"StockTick 서버가 이미 실행 중이에요 (http://localhost:{port}). 새로 시작하지 않고 종료합니다.", flush=True)
        if FROZEN:
            open_app_window()   # 실행 파일을 또 눌렀으면 기존 서버에 연결된 창만 하나 더
        time.sleep(3)   # 메시지를 읽을 시간
        sys.exit(0)
    print(f"다른 프로그램이 {port}번 포트를 쓰고 있어서 StockTick 서버를 시작할 수 없어요.", flush=True)
    print("그 프로그램을 종료하거나, .env에 STOCKTICK_PORT=5050 처럼 다른 포트를 지정한 뒤 다시 실행해 주세요.", flush=True)
    if sys.platform == "darwin" and port == 5000:
        print("(맥이라면 시스템 설정 > 일반 > AirDrop 및 Handoff > 'AirPlay 수신 모드'가 5000번을 쓰고 있을 수 있어요.)", flush=True)
    time.sleep(15)
    sys.exit(1)


if __name__ == "__main__":
    _exit_if_already_running(PORT)

from flask import Flask, jsonify, request, Response

import kis_client

app = Flask(__name__, static_folder=res_path("static"))


# 2초·10초·1분마다 자동으로 오는 갱신 요청. KIS 호출 순서에서 화면 요청(차트·종목 정보 등)보다 뒤로 미룸
_BACKGROUND_PATHS = ("/api/quotes", "/api/kr-indices", "/api/us-indices")


@app.before_request
def _mark_request():
    global _last_request
    _last_request = time.time()
    kis_client.set_background(request.path in _BACKGROUND_PATHS)

import stock_master

with open(res_path("stock_list.json"), encoding="utf-8") as f:
    STOCK_LIST = json.load(f)  # [{name, code, market}, ...] - 마스터 로딩 전/실패 시 쓰는 기본 목록


def _load_master():
    """전체 종목 마스터를 백그라운드로 로딩해서 STOCK_LIST 교체 (서버 시작을 막지 않도록)"""
    global STOCK_LIST
    data = stock_master.load()
    if data:
        STOCK_LIST = data
        print(f"[MASTER] 검색 목록 교체: {len(data)}건 ({stock_master.last_info['source']})", flush=True)
    else:
        print("[MASTER] 마스터를 못 받아 내장 stock_list.json으로 검색합니다.", flush=True)


threading.Thread(target=_load_master, daemon=True).start()


def _norm(t):
    return t.replace(" ", "").upper()


@app.route("/api/search")
def search():
    """종목명/코드로 검색 (외부 API 없이 내장 리스트에서 검색) -> [{name, code, market}]"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    nq = _norm(q)
    ranked = []
    for s in STOCK_LIST:
        nm = _norm(s["name"])
        if nm == nq or s["code"] == q:
            rank = 0          # 정확히 일치
        elif nm.startswith(nq):
            rank = 1          # 앞부분 일치
        elif nq in nm or q in s["code"]:
            rank = 2          # 포함
        else:
            continue
        ranked.append((rank, len(s["name"]), s))
    ranked.sort(key=lambda x: (x[0], x[1]))
    results = [x[2] for x in ranked]

    # 6자리 숫자 코드를 직접 입력했는데 리스트에 없는 경우, KIS API로 이름 조회해서 추가
    if re.fullmatch(r"\d{6}", q) and not any(s["code"] == q for s in results):
        name = kis_client.get_stock_name(q)
        results.insert(0, {
            "name": name if name else f"코드 {q} (이름 조회 실패)",
            "code": q,
            "market": "?",
        })

    return jsonify(results[:15])


@app.route("/api/quotes")
def quotes():
    """콤마로 구분된 종목코드 리스트 -> KIS 웹소켓 캐시에서 최신 체결가 반환.
    웹소켓 실시간 틱이 아직 없으면(장마감 등) REST로 종가/마지막 체결가를 대신 조회함."""
    codes = request.args.get("codes", "").strip()
    if not codes:
        return jsonify([])

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    out = []
    for code in code_list:
        kis_client.ensure_subscribed(code)
        name = next((s["name"] for s in STOCK_LIST if s["code"] == code), None)

        tick = kis_client.get_quote(code)
        if tick:
            out.append({**tick, "name": name})
            continue

        rest = kis_client.get_rest_quote(code)
        if rest:
            out.append({**rest, "name": name})
            continue

        out.append({
            "code": code, "name": name, "price": None, "change": None,
            "rate": None, "open": None, "high": None, "low": None,
            "volume": None, "sign": None, "live": False,
        })

    return jsonify(out)


@app.route("/api/history")
def history():
    """기간별 시세: ?code=005930&start=20260101&end=20261126&period=D(일)|W(주)|M(월)|Y(년)
    -> {"code", "name", "period", "bars": [{time, open, high, low, close, volume}, ...]} (오래된 순)"""
    code = request.args.get("code", "").strip().upper()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip() or kis_client.today_kst()
    period = request.args.get("period", "D").strip().upper()
    adj = "1" if request.args.get("adj") == "1" else "0"  # 기본은 수정주가

    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return jsonify({"error": "종목코드는 6자리여야 해요."}), 400
    if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)):
        return jsonify({"error": "날짜는 YYYYMMDD 형식이어야 해요."}), 400
    if start > end:
        return jsonify({"error": "시작일이 종료일보다 늦어요."}), 400
    if period not in ("D", "W", "M", "Y"):
        return jsonify({"error": "period는 D, W, M, Y 중 하나예요."}), 400

    try:
        data = kis_client.get_history(code, start, end, period, adj)
    except Exception as e:
        print(f"[KIS REST] 기간별 시세 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"시세 조회 실패: {e}"}), 502

    name = data["name"] or next((s["name"] for s in STOCK_LIST if s["code"] == code), None)
    return jsonify({"code": code, "name": name, "period": period, **{k: data[k] for k in ("bars", "pages")}})


@app.route("/api/info")
def stock_info():
    """종목 정보: 52주 최고/최저, 기준가, 거래대금, 시가총액, 외국인 소진율, PER/PBR/EPS/BPS, 업종"""
    code = request.args.get("code", "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return jsonify({"error": "종목코드는 6자리여야 해요."}), 400
    kis_client.set_priority(code)   # 상세 화면에서 보는 종목은 실시간 구독 우선
    try:
        return jsonify(kis_client.get_stock_info(code))
    except Exception as e:
        print(f"[KIS REST] 종목 정보 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"종목 정보 조회 실패: {e}"}), 502


@app.route("/api/investor")
def investor():
    """투자자별 매매동향: ?code=353200&date=YYYYMMDD(생략 시 오늘)
    date부터 과거로 30거래일의 개인/외국인/기관 순매수 수량(주), 최신순"""
    code = request.args.get("code", "").strip().upper()
    date = request.args.get("date", "").strip() or kis_client.today_kst()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return jsonify({"error": "종목코드는 6자리여야 해요."}), 400
    if not re.fullmatch(r"\d{8}", date):
        return jsonify({"error": "날짜는 YYYYMMDD 형식이어야 해요."}), 400
    try:
        return jsonify(kis_client.get_investor_daily(code, date))   # {"rows": [...], "market": "UN" 또는 "J"}
    except Exception as e:
        print(f"[KIS REST] 투자자 매매동향 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"투자자 매매동향 조회 실패: {e}"}), 502


@app.route("/api/investor-debug")
def investor_debug():
    """투자자 매매동향 원본 응답 확인용 (필드명/단위 검증)"""
    code = request.args.get("code", "005930")
    try:
        raw = kis_client._fetch_investor_raw(code, request.args.get("market", "J").upper())
        rows = raw.get("output") or []
        out = {"rt_cd": raw.get("rt_cd"), "msg1": raw.get("msg1"), "row_count": len(rows),
               "first_row": rows[0] if rows else None, "second_row": rows[1] if len(rows) > 1 else None}
        # 보유율용 일자별 시세 API 원본도 함께 (필드명 확인용)
        try:
            dp = kis_client._fetch_daily_price_raw(code)
            dprows = dp.get("output") or []
            out["daily_price"] = {"rt_cd": dp.get("rt_cd"), "msg1": dp.get("msg1"), "row_count": len(dprows),
                                  "first_row": dprows[0] if dprows else None}
        except Exception as e:
            out["daily_price"] = {"error": f"{type(e).__name__}: {e}"}
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@app.route("/api/investor-daily-debug")
def investor_daily_debug():
    """[검증용] 날짜 지정 투자자매매동향 API의 원본 응답 - 필드명, 건수, 날짜 범위 확인"""
    code = request.args.get("code", "353200")
    date = request.args.get("date", kis_client.today_kst())
    market = request.args.get("market", "J").upper()   # J: KRX, NX: NXT, UN: 통합
    try:
        status, raw = kis_client._fetch_investor_daily_raw(code, date, market)
        out = {"market": market, "http_status": status, "rt_cd": raw.get("rt_cd"), "msg_cd": raw.get("msg_cd"), "msg1": raw.get("msg1")}
        # 네이버와 비교하기 쉽게: 날짜 / 외국인 / 기관 / 개인 순매수만 10줄
        rows = raw.get("output2") if isinstance(raw.get("output2"), list) else []
        out["compare"] = [f"{r.get('stck_bsop_date')}  외국인 {r.get('frgn_ntby_qty')}  기관 {r.get('orgn_ntby_qty')}  개인 {r.get('prsn_ntby_qty')}"
                          for r in rows[:10]]
        for key in ("output", "output1", "output2"):
            v = raw.get(key)
            if isinstance(v, list):
                out[key] = {"row_count": len(v), "first_row": v[0] if v else None, "last_row": v[-1] if v else None}
            elif v is not None:
                out[key] = v
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@app.route("/api/nxt")
def nxt_quote():
    """NXT(넥스트레이드) 현재가 - KRX가 닫혀 있는 프리마켓/애프터마켓 시간에 표시용"""
    code = request.args.get("code", "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return jsonify({"error": "종목코드는 6자리여야 해요."}), 400
    try:
        return jsonify(kis_client.get_nxt_quote(code))
    except Exception as e:
        print(f"[KIS REST] NXT 시세 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"NXT 시세 조회 실패: {e}"}), 502


@app.route("/api/nxt-debug")
def nxt_debug():
    """[확인용] 같은 종목의 KRX(J) / NXT(NX) / 통합(UN) 현재가 비교"""
    code = request.args.get("code", "005380")
    out = {}
    for m in ("J", "NX", "UN"):
        try:
            status, body = kis_client._fetch_price_raw(code, m)
            o = body.get("output") or {}
            out[m] = {"http": status, "rt_cd": body.get("rt_cd"), "msg1": body.get("msg1"),
                      "현재가": o.get("stck_prpr"), "전일대비": o.get("prdy_vrss"), "등락률": o.get("prdy_ctrt"),
                      "누적거래량": o.get("acml_vol"), "기준가": o.get("stck_sdpr")}
        except Exception as e:
            out[m] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)


@app.route("/api/expected")
def expected():
    """동시호가 예상체결가 (장 시작 08:30~09:00, 장 마감 15:20~15:30에 표시용)"""
    code = request.args.get("code", "").strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return jsonify({"error": "종목코드는 6자리여야 해요."}), 400
    try:
        return jsonify(kis_client.get_expected(code))
    except Exception as e:
        print(f"[KIS REST] 예상체결 조회 실패 ({code}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"예상체결 조회 실패: {e}"}), 502


@app.route("/api/expected-debug")
def expected_debug():
    """[확인용] 동시호가 예상체결가 원본. output2에 예상체결 정보가 있을 것으로 예상 (필드명 확인용)"""
    code = request.args.get("code", "005930")
    market = request.args.get("market", "J").upper()
    try:
        status, body = kis_client._fetch_expected_raw(code, market)
        o1 = body.get("output1") or {}
        return jsonify({
            "확인시각": time.strftime("%H:%M:%S"), "market": market, "http": status,
            "rt_cd": body.get("rt_cd"), "msg_cd": body.get("msg_cd"), "msg1": body.get("msg1"),
            "output2": body.get("output2"),                       # 예상체결가 등 (전체)
            "output1_keys": sorted(o1.keys()) if isinstance(o1, dict) else None,   # 호가 필드 목록만
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


# ---------- 관심종목 그룹 (서버 파일에 저장: 브라우저/프로필이 바뀌어도 유지) ----------
WATCH_FILE = app_path("watchlists.json")


def _valid_watchlists(d):
    """{"groups": [{"name": str, "items": [{"code","name","market"}]}], "active": int} 형태인지 검사"""
    if not isinstance(d, dict) or not isinstance(d.get("groups"), list) or not (1 <= len(d["groups"]) <= 50):
        return False
    for g in d["groups"]:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not (1 <= len(g["name"]) <= 30):
            return False
        items = g.get("items")
        if not isinstance(items, list) or len(items) > 300:
            return False
        for it in items:
            if not isinstance(it, dict) or not re.fullmatch(r"[0-9A-Z]{6}", str(it.get("code", ""))):
                return False
            if not isinstance(it.get("name"), str) or len(it["name"]) > 60:
                return False
    return isinstance(d.get("active", 0), int)


@app.route("/api/watchlists", methods=["GET", "PUT"])
def watchlists():
    if request.method == "GET":
        try:
            with open(WATCH_FILE, encoding="utf-8") as f:
                return jsonify(json.load(f))
        except FileNotFoundError:
            return jsonify({})   # 아직 저장된 적 없음 -> 브라우저에 있던 목록을 올려 보내게 됨
        except Exception as e:
            return jsonify({"error": f"관심종목 파일을 읽지 못했어요: {e}"}), 500
    d = request.get_json(silent=True)
    if not _valid_watchlists(d):
        return jsonify({"error": "관심종목 형식이 올바르지 않아요."}), 400
    d["active"] = max(0, min(d.get("active", 0), len(d["groups"]) - 1))
    tmp = WATCH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, WATCH_FILE)   # 쓰는 도중 꺼져도 기존 파일이 깨지지 않게
    return jsonify({"ok": True})


@app.route("/api/index-history")
def index_history():
    """지수 차트: ?key=kospi|kosdaq|dji|spx|comp|sox&start=YYYYMMDD&end=YYYYMMDD&period=D|W|M"""
    key = request.args.get("key", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "") or kis_client.today_kst()
    period = request.args.get("period", "D").upper()
    if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)) or start > end or period not in ("D", "W", "M"):
        return jsonify({"error": "기간 형식이 올바르지 않아요."}), 400
    try:
        return jsonify(kis_client.get_index_history(key, start, end, period))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"[KIS REST] 지수 차트 조회 실패 ({key}): {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"지수 시세 조회 실패: {e}"}), 502


@app.route("/api/kr-indices")
def kr_indices():
    """국내 지수: 코스피, 코스닥"""
    try:
        return jsonify(kis_client.get_kr_indices())
    except Exception as e:
        return jsonify({"error": f"국내 지수 조회 실패: {e}"}), 502


@app.route("/api/kr-index-debug")
def kr_index_debug():
    """[확인용] 코스피(0001) 현재지수와 일별 시세 원본"""
    from datetime import datetime, timedelta
    code = request.args.get("code", "0001")
    end = kis_client.today_kst()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    out = {}
    try:
        status, body = kis_client._fetch_kr_index_raw(code)
        out["현재지수"] = {"http": status, "rt_cd": body.get("rt_cd"), "msg1": body.get("msg1"), "output": body.get("output")}
    except Exception as e:
        out["현재지수"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        status, body = kis_client._fetch_kr_index_daily_raw(code, start, end)
        rows = body.get("output2") if isinstance(body.get("output2"), list) else []
        out["일별"] = {"http": status, "rt_cd": body.get("rt_cd"), "msg1": body.get("msg1"),
                       "행수": len(rows), "첫행": rows[0] if rows else None}
    except Exception as e:
        out["일별"] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)


@app.route("/api/qqq")
def qqq():
    """나스닥100 ETF(QQQ): 한국 낮에는 주간거래, 그 밖에는 나스닥(프리·정규장) 가격"""
    try:
        return jsonify(kis_client.get_qqq())
    except Exception as e:
        return jsonify({"error": f"QQQ 조회 실패: {e}"}), 502


@app.route("/api/qqq-debug")
def qqq_debug():
    """[확인용] QQQ(나스닥100 ETF)를 주간거래(BAQ)와 정규(NAS) 시장 코드로 조회해서 응답 비교"""
    symb = request.args.get("symb", "QQQ").upper()
    out = {"확인시각": time.strftime("%H:%M:%S"), "종목": symb}
    for excd in ("BAQ", "NAS"):
        try:
            status, body = kis_client._fetch_overseas_price_raw(excd, symb)
            out[excd] = {"http": status, "rt_cd": body.get("rt_cd"), "msg_cd": body.get("msg_cd"),
                         "msg1": body.get("msg1"), "output": body.get("output")}
        except Exception as e:
            out[excd] = {"error": f"{type(e).__name__}: {e}"}
        time.sleep(0.5)   # 지금 키의 낮은 호출 한도 고려
    return jsonify(out)


@app.route("/api/us-indices")
def us_indices():
    """미국 지수: 다우, S&P500, 나스닥, 필라델피아 반도체"""
    try:
        return jsonify(kis_client.get_us_indices())
    except Exception as e:
        return jsonify({"error": f"미국 지수 조회 실패: {e}"}), 502


@app.route("/api/us-index-debug")
def us_index_debug():
    """[확인용] 미국 지수별 후보 코드를 모두 시도해서 어떤 코드가 응답하는지 확인"""
    from datetime import datetime, timedelta
    end = kis_client.today_kst()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    candidates = {
        "다우": [".DJI", "DJI", "DJIA"],
        "S&P500": ["SPX", ".INX", "SPX500"],
        "나스닥": ["COMP", ".IXIC", "IXIC"],
        "필라델피아반도체": ["SOX", ".SOX", "PHLX"],
    }
    if request.args.get("code"):   # 직접 코드 하나를 시험할 때: ?code=XXX
        candidates = {"직접": [request.args["code"]]}
    out = {"조회기간": f"{start}~{end}"}
    for name, codes in candidates.items():
        out[name] = {}
        for c in codes:
            try:
                status, body = kis_client._fetch_overseas_index_raw(c, start, end)
                rows = body.get("output2") if isinstance(body.get("output2"), list) else []
                out[name][c] = {"http": status, "rt_cd": body.get("rt_cd"), "msg1": body.get("msg1"),
                                "output1": body.get("output1"), "행수": len(rows),
                                "최근행": rows[0] if rows else None}
            except Exception as e:
                out[name][c] = {"error": f"{type(e).__name__}: {e}"}
            time.sleep(0.1)   # REST 초당 호출 제한 여유
    return jsonify(out)


@app.route("/api/rate-test")
def rate_test():
    """[확인용] 지금 앱키의 실제 초당 호출 한도와 응답 속도 측정.
    삼성전자 현재가 조회를 초당 2/5/10/20건 속도로 10번씩 보냄 (속도 조절 없이 직접). 약 15초 걸림"""
    import requests as _rq
    sess = _rq.Session()
    sess.trust_env = False
    token = kis_client._get_access_token()
    headers = {"authorization": f"Bearer {token}", "appkey": kis_client.APP_KEY, "appsecret": kis_client.APP_SECRET,
               "tr_id": "FHKST01010100", "custtype": "P"}
    url = f"{kis_client.REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}
    out = {"앱키_앞자리": kis_client.APP_KEY[:4] + "...", "측정시각": time.strftime("%H:%M:%S")}
    for rps in (2, 5, 10, 20):
        ok = limited = other = 0
        lat = []
        other_msgs = set()
        for _ in range(10):
            t0 = time.monotonic()
            try:
                r = sess.get(url, headers=headers, params=params, timeout=5)
                lat.append(time.monotonic() - t0)
                # rt_cd는 응답 끝에 있으므로 JSON으로 읽어서 판정
                try:
                    j = r.json()
                except ValueError:
                    j = {}
                if "EGW00201" in r.text or "초당 거래건수" in r.text:
                    limited += 1
                elif r.status_code == 200 and j.get("rt_cd") == "0":
                    ok += 1
                else:
                    other += 1
                    other_msgs.add(f"HTTP {r.status_code} [{j.get('msg_cd')}] {j.get('msg1') or r.text[:60]}")
            except Exception as e:
                other += 1
                other_msgs.add(f"{type(e).__name__}")
            time.sleep(max(0.0, 1.0 / rps - (time.monotonic() - t0)))
        out[f"초당 {rps}건"] = {"성공": ok, "한도초과": limited, "기타오류": other,
                              "평균응답(초)": round(sum(lat) / len(lat), 3) if lat else None,
                              **({"기타오류내용": sorted(other_msgs)[:3]} if other_msgs else {})}
        time.sleep(1.5)   # 다음 측정 전에 한도 회복
    return jsonify(out)


@app.route("/api/master-status")
def master_status():
    """종목 마스터 로딩 상태 확인용"""
    return jsonify({**stock_master.last_info, "search_list_size": len(STOCK_LIST),
                    "sample": [s for s in STOCK_LIST if "대덕" in s["name"]][:5]})


@app.route("/api/history-debug")
def history_debug():
    """기간별시세 1페이지 원본 응답을 그대로 노출 (필드/정렬/건수 검증용)"""
    code = request.args.get("code", "005930")
    end = request.args.get("end", kis_client.today_kst())
    start = request.args.get("start", "20250101")
    period = request.args.get("period", "D")
    try:
        raw = kis_client._fetch_chart_page(code, start, end, period, "0")
        rows = raw.get("output2") or []
        return jsonify({
            "rt_cd": raw.get("rt_cd"), "msg1": raw.get("msg1"),
            "row_count": len(rows),
            "first_row": rows[0] if rows else None,
            "last_row": rows[-1] if rows else None,
            "output1": raw.get("output1"),
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@app.route("/api/ws-raw-debug")
def ws_raw_debug():
    """웹소켓으로 받은 원본 필드 그대로 노출 (부호 규칙 검증용)"""
    code = request.args.get("code", "")
    raw = kis_client.LAST_RAW_TICK.get(code)
    if raw is None:
        return jsonify({"error": "아직 이 종목의 실시간 틱을 못 받았음"})
    return jsonify({
        "code": raw[0], "time": raw[1], "price(2)": raw[2], "sign(3)": raw[3],
        "change(4)": raw[4], "rate(5)": raw[5], "all_fields": raw,
    })


@app.route("/api/stock-name-debug")
def stock_name_debug():
    """종목명 조회용 후보 API(상품기본조회)를 시험 호출 - 검증 전이라 원본 응답 그대로 노출"""
    code = request.args.get("code", "000660")
    try:
        token = kis_client._get_access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": kis_client.APP_KEY,
            "appsecret": kis_client.APP_SECRET,
            "tr_id": "CTPF1002R",
            "custtype": "P",
        }
        params = {"PDNO": code, "PRDT_TYPE_CD": "300"}
        r = kis_client._rest_session.get(
            f"{kis_client.REST_BASE}/uapi/domestic-stock/v1/quotations/search-info",
            headers=headers, params=params, timeout=8,
        )
        return jsonify({"status_code": r.status_code, "raw": r.json()})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@app.route("/api/rest-quote-debug")
def rest_quote_debug():
    """REST 종가 조회의 원본 응답을 그대로 노출 (파싱 전/후 비교용)"""
    code = request.args.get("code", "005930")
    import time as _time
    try:
        token = kis_client._get_access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": kis_client.APP_KEY,
            "appsecret": kis_client.APP_SECRET,
            "tr_id": "FHKST01010100",
            "custtype": "P",
        }
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        t0 = _time.time()
        r = kis_client._rest_session.get(
            f"{kis_client.REST_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers, params=params, timeout=8,
        )
        raw = r.json()
        parsed = kis_client.get_rest_quote(code)
        return jsonify({"elapsed_sec": round(_time.time()-t0,2), "raw_output": raw.get("output"), "parsed": parsed})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@app.route("/api/kis-restart")
def kis_restart():
    """재배포 없이 KIS 연결 스레드를 강제로 새로 시작 (디버깅용)"""
    kis_client.restart()
    return jsonify({"ok": True, "message": "재시작 요청함, 몇 초 후 /api/kis-status 확인"})


@app.route("/api/kis-approval-direct-test")
def kis_approval_direct_test():
    """백그라운드 스레드가 실제로 사용하는 kis_client._get_approval_key()를
    그대로 호출해서 그 함수/세션 자체에 문제가 있는지 직접 확인"""
    import time as _time
    t0 = _time.time()
    try:
        key = kis_client._get_approval_key()
        return jsonify({"ok": True, "elapsed_sec": round(_time.time() - t0, 2), "key_preview": key[:8] + "..."})
    except Exception as e:
        return jsonify({"ok": False, "elapsed_sec": round(_time.time() - t0, 2), "error": f"{type(e).__name__}: {e}"})


@app.route("/api/net-test")
def net_test():
    """Shell 없이도 서버에서 직접 아웃바운드 네트워크 상태를 진단"""
    import os as _os
    import socket
    import time as _time

    import requests as _requests

    results = {}

    # 0) 프록시 관련 환경변수가 설정되어 있는지 확인 (있으면 requests가 자동으로 그쪽을 타려다 멈출 수 있음)
    results["proxy_env"] = {
        k: _os.environ.get(k) for k in
        ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
        if _os.environ.get(k)
    }

    session = _requests.Session()
    session.trust_env = False  # 환경변수의 프록시 설정을 무시하도록 강제

    # 1) REST API 포트(HTTPS, 9443) - DNS + TCP + TLS까지
    t0 = _time.time()
    try:
        r = session.get("https://openapi.koreainvestment.com:9443/", timeout=8)
        results["https_9443"] = {"ok": True, "status": r.status_code, "elapsed_sec": round(_time.time() - t0, 2)}
    except Exception as e:
        results["https_9443"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_sec": round(_time.time() - t0, 2)}

    # 2) 웹소켓 포트(21000) - 순수 TCP 연결만 테스트 (프로토콜 핸드셰이크 전)
    t0 = _time.time()
    try:
        s = socket.create_connection(("ops.koreainvestment.com", 21000), timeout=8)
        s.close()
        results["tcp_21000"] = {"ok": True, "elapsed_sec": round(_time.time() - t0, 2)}
    except Exception as e:
        results["tcp_21000"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_sec": round(_time.time() - t0, 2)}

    # 3) DNS 조회 자체가 되는지 (앞의 둘이 다 실패하면 이걸로 원인 좁히기)
    t0 = _time.time()
    try:
        ip = socket.gethostbyname("ops.koreainvestment.com")
        results["dns"] = {"ok": True, "resolved_ip": ip, "elapsed_sec": round(_time.time() - t0, 2)}
    except Exception as e:
        results["dns"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_sec": round(_time.time() - t0, 2)}

    # 4) 실제 approval_key 발급 요청을 여기서 직접 한번 호출 (백그라운드 스레드가 아닌
    #    일반 요청 컨텍스트에서도 똑같이 멈추는지 확인용 - 키/시크릿 값 자체는 응답에 노출 안 함)
    t0 = _time.time()
    try:
        body = {
            "grant_type": "client_credentials",
            "appkey": kis_client.APP_KEY,
            "secretkey": kis_client.APP_SECRET,
        }
        r = session.post(f"{kis_client.REST_BASE}/oauth2/Approval", json=body, timeout=8)
        ok = r.status_code == 200 and "approval_key" in r.json()
        results["oauth_approval"] = {
            "ok": ok, "status": r.status_code,
            "elapsed_sec": round(_time.time() - t0, 2),
            "body_preview": r.text[:150],
        }
    except Exception as e:
        results["oauth_approval"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_sec": round(_time.time() - t0, 2)}

    return jsonify(results)


@app.route("/api/kis-status")
def kis_status():
    """진단용: 웹소켓 연결 단계, 마지막 에러, 캐시 원본을 그대로 보여줌"""
    return jsonify({**kis_client.get_status(), "cache": kis_client.QUOTE_CACHE})


@app.route("/manifest.json")
def manifest():
    with open(res_path("manifest.json"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    with open(res_path("sw.js"), encoding="utf-8") as f:
        # 서비스워커는 루트 스코프여야 전체 앱을 제어할 수 있음
        return Response(f.read(), mimetype="application/javascript")


@app.route("/")
def index():
    with open(res_path("index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


kis_client.start_background()

if __name__ == "__main__":
    if FROZEN:
        # 실행 파일: 앱 창을 직접 띄움 (배치 파일 불필요). 개발 중(python app.py)에는 배치 파일이 띄움
        print(f"StockTick 서버를 시작해요 ({APP_URL}). 앱 창을 닫으면 약 {IDLE_EXIT_SEC // 60}분 뒤 서버도 종료돼요.", flush=True)
        threading.Thread(target=_open_when_ready, daemon=True).start()
        threading.Thread(target=_idle_watchdog, daemon=True).start()
        # 2초마다 오는 시세 요청 기록은 로그가 커지기만 하므로 경고 이상만 남김
        import logging
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # debug=True의 자동 재시작 기능은 프로세스를 두 번 띄워서
    # KIS 웹소켓 세션도 두 번 생성되는 문제가 있어 reloader는 꺼둠.
    # 실행 파일에서는 디버그 모드를 끔 (오류 화면에 내부 정보가 노출되지 않게)
    app.run(host="0.0.0.0", port=PORT, debug=not FROZEN, use_reloader=False)
