/* CafePOS service worker.
 * Что делает:
 *   1. файлы оформления (/static/): сначала сеть, при обрыве связи берёт последнюю копию;
 *   2. переход на страницу без связи: вместо ошибки браузера показывает страницу «Нет связи с кассой».
 * Чего не делает: не хранит заказы, суммы, страницы и ответы кассы. Все запросы с данными
 * (HTMX, формы, оплата, /ping/) идут напрямую на сервер, как без приложения. */
const VERSION = '__VERSION__';
const CACHE = 'cafepos-' + VERSION;
const OFFLINE_URL = '/offline/';
const PRECACHE = [OFFLINE_URL, '/static/icons/icon-192.png', '/static/icons/favicon-32.png'];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) { return cache.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (key) { return key.indexOf('cafepos-') === 0 && key !== CACHE; })
          .map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Открытие страницы: если сервер недоступен, показываем страницу «нет связи»
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(function () { return caches.match(OFFLINE_URL); })
    );
    return;
  }

  // Файлы оформления: сеть, а без сети последняя сохранённая копия
  if (url.pathname.indexOf('/static/') === 0) {
    event.respondWith(
      fetch(request).then(function (response) {
        if (response && response.status === 200) {
          const copy = response.clone();
          caches.open(CACHE).then(function (cache) { cache.put(request, copy); });
        }
        return response;
      }).catch(function () { return caches.match(request); })
    );
  }
  // Всё остальное (данные, HTMX, оплата, /ping/) не перехватываем
});
