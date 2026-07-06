import json
import re

from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv

load_dotenv()  # .env 파일에서 KIS_APP_KEY / KIS_APP_SECRET 로드

import kis_client

app = Flask(__name__)

with open("stock_list.json", encoding="utf-8") as f:
    STOCK_LIST = json.load(f)  # [{name, code, market}, ...]


@app.route("/api/search")
def search():
    """종목명/코드로 검색 (외부 API 없이 내장 리스트에서 검색) -> [{name, code, market}]"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    results = [s for s in STOCK_LIST if q in s["name"] or q in s["code"]]

    # 6자리 숫자 코드를 직접 입력했는데 리스트에 없는 경우, 그대로 추가할 수 있게 허용
    if re.fullmatch(r"\d{6}", q) and not any(s["code"] == q for s in results):
        results.insert(0, {"name": f"코드 {q} (직접입력)", "code": q, "market": "?"})

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
    with open("manifest.json", encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    with open("sw.js", encoding="utf-8") as f:
        # 서비스워커는 루트 스코프여야 전체 앱을 제어할 수 있음
        return Response(f.read(), mimetype="application/javascript")


@app.route("/")
def index():
    with open("index.html", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


kis_client.start_background()

if __name__ == "__main__":
    # debug=True의 자동 재시작 기능은 프로세스를 두 번 띄워서
    # KIS 웹소켓 세션도 두 번 생성되는 문제가 있어 reloader는 꺼둠.
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
