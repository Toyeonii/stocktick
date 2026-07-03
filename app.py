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
    아직 구독 전이거나 첫 틱이 안 들어온 종목은 price=None으로 내려감(프론트에서 '-' 처리)."""
    codes = request.args.get("codes", "").strip()
    if not codes:
        return jsonify([])

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    out = []
    for code in code_list:
        kis_client.ensure_subscribed(code)
        tick = kis_client.get_quote(code)
        name = next((s["name"] for s in STOCK_LIST if s["code"] == code), None)
        if tick:
            out.append({**tick, "name": name})
        else:
            out.append({
                "code": code, "name": name, "price": None, "change": None,
                "rate": None, "open": None, "high": None, "low": None,
                "volume": None, "sign": None,
            })

    return jsonify(out)


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
