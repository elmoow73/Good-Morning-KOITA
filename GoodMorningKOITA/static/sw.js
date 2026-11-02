// GoodMorningKOITA 서비스 워커: 앱 설치(PWA)를 위한 최소 구성.
// 기사·키워드는 늘 서버의 최신 값을 써야 하므로 아무것도 캐시하지 않고 그대로 서버에 묻는다.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
