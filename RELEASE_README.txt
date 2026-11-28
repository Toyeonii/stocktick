StockTick 실행 방법
====================

1. 처음 한 번만: .env 파일 준비
   - .env.example 을 복사해서 이름을 .env 로 바꾸고,
     KIS_APP_KEY / KIS_APP_SECRET 에 한국투자증권 Open API 키를 넣으세요.
   - .env 는 반드시 StockTick.exe 와 같은 폴더에 있어야 합니다.

2. StockTick.exe 를 더블클릭
   - 몇 초 뒤 웨일(없으면 엣지) 앱 창이 열립니다. 서버는 창 없이 뒤에서 돌아갑니다.
   - 앱 창을 닫으면 약 3분 뒤 서버도 자동으로 종료됩니다.
   - 3분 안에 StockTick.exe 를 다시 누르면 켜져 있는 서버에 연결된 창만 다시 열립니다.
   - 바로 끄고 싶으면 작업 관리자에서 StockTick 을 종료하세요.

3. 실행하면 같은 폴더에 자동으로 생기는 파일 (지우지 마세요)
   - .kis_token.json   : KIS 접근 토큰 (재실행 시 재사용, 발급 횟수 제한 회피)
   - watchlists.json   : 관심종목 그룹
   - stock_master.json : 전체 종목 목록 (하루 한 번 갱신)
   - whale-profile\    : 앱 창 전용 브라우저 프로필 (창 크기 등)
   - stocktick.log     : 서버 기록 (문제가 생기면 이 파일 내용을 확인)

4. 다른 PC로 옮길 때
   - StockTick.exe, .env, (관심종목 유지하려면) watchlists.json 세 개만 복사하면 됩니다.

참고
- 회사망에서는 KIS 서버(포트 9443, 21000)가 막혀 시세가 안 나올 수 있습니다.
- 앱 창은 화면에 맞게 최대화로 열립니다(작업 표시줄과 겹치지 않음). 특정 크기로 열려면 .env 에 STOCKTICK_WINDOW_SIZE=1536,1112 처럼 추가하세요.
- 다른 브라우저로 열려면 .env 에 STOCKTICK_BROWSER=브라우저 exe 전체 경로 를 추가하세요.
