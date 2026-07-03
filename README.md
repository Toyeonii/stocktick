# StockTick — 광고 없는 관심종목 실시간 시세 (한국투자증권 Open API)

증권통 같은 앱의 광고 없이, 관심종목만 깔끔하게 보고 싶어서 만든 초경량 웹앱.
한국투자증권 KIS Developers 웹소켓으로 실시간 체결가를 직접 받아옵니다 (비공식 스크래핑 없음).

## 구조
- `app.py` — Flask 백엔드
- `kis_client.py` — KIS 웹소켓 연결/구독/실시간 캐싱을 담당하는 백그라운드 스레드
- `index.html` — 단일 파일 프론트엔드 (검색 + 관심종목 카드 + 2초마다 화면 갱신)
- `stock_list.json` — 검색용 내장 종목 리스트 (외부 API 불필요)
- 관심종목 리스트는 브라우저 `localStorage`에 저장 (서버 DB 없음)

## 최초 1회 설정

1. `.env.example`을 복사해서 `.env`로 이름 변경
2. 한국투자증권에서 발급받은 App Key / App Secret을 `.env`에 채워넣기
   ```
   KIS_APP_KEY=발급받은_키
   KIS_APP_SECRET=발급받은_시크릿
   KIS_VIRTUAL=0
   ```
3. `.env` 파일은 **절대 git에 커밋하지 마세요** (`.gitignore`에 이미 포함됨)

## 로컬 실행
```bash
cd stocktick
pip install -r requirements.txt
python app.py
```
브라우저에서 http://localhost:5000 접속. (한국투자증권 웹소켓은 국내 주식시장 운영시간(평일 09:00~15:30)에만 체결가가 흐릅니다. 장 시간 외에는 가격이 "-"로 표시될 수 있어요.)

## Render 배포
1. GitHub 레포 생성 후 이 폴더 push (`.env`는 제외됨, `render.yaml` 포함)
2. Render.com → New → Blueprint → 방금 만든 레포 선택 (`render.yaml` 자동 인식)
3. 배포 과정에서 `KIS_APP_KEY`, `KIS_APP_SECRET` 입력창이 뜸 → 발급받은 값 입력
   - Blueprint 대신 수동으로 Web Service를 만들었다면, Render 대시보드 → Environment 탭에서 직접 등록
4. 배포 완료되면 `https://stocktick-xxxx.onrender.com` 같은 주소 생성됨
5. gunicorn은 `--workers 1`로 고정되어 있음 (KIS 웹소켓 세션이 워커 수만큼 중복 생성되는 걸 방지)

## PWA로 설치하기
배포된 주소에 브라우저로 접속한 뒤:
- **iOS(Safari)**: 공유 버튼 → "홈 화면에 추가"
- **Android(Chrome)**: 메뉴(⋮) → "앱 설치" 또는 "홈 화면에 추가"
- **PC(Chrome/Edge)**: 주소창 오른쪽의 설치 아이콘 클릭

설치하면 광고 없는 독립 앱처럼 아이콘이 생기고, 브라우저 주소창 없이 전체화면으로 실행됩니다.

## 동작 방식
- 서버가 시작되면 KIS 웹소켓에 접속해서 세션을 하나 유지
- 관심종목을 추가하면 서버가 그 종목을 웹소켓에 구독 등록
- 실시간으로 들어오는 체결가를 서버 메모리에 캐싱
- 프론트는 2초마다 `/api/quotes`를 호출해서 캐시된 값을 받아 화면 갱신
- 세션 당 구독 가능 종목 수는 대략 40개 내외로 제한되어 있음 (KIS 정책, 변동 가능)

## 참고사항
- 웹소켓 approval_key 발급 실패, 연결 끊김 등은 5초 간격으로 자동 재시도됨
- `stock_list.json`에 원하는 종목을 직접 추가/편집해서 검색 결과를 늘릴 수 있음
- 매매(주문) 기능은 없음 — 시세 조회 전용

## 다음에 붙일 수 있는 것들
- 종목 드래그 정렬
- 관심종목 그룹(국내/해외 탭) — 해외는 별도 tr_id 필요
- PWA manifest 추가해서 홈 화면에 앱처럼 설치
