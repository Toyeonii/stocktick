const CACHE_NAME = "stocktick-shell-v1";
const SHELL_URLS = ["/", "/manifest.json", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API 요청(검색/시세)은 항상 네트워크에서 새로 받아옴 - 캐싱하지 않음
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // 그 외(앱 셸)는 네트워크 우선, 실패 시 캐시로 폴백 (오프라인 대비)
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
