// sw.js - Service Worker für Mobile.de Push Notifications
self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { title: 'Mobile.de', body: e.data ? e.data.text() : '' }; }

  const title = data.title || 'Autoscan Mobile';
  const opts = {
    body: data.body || '',
    icon: '/mobile/static/icons/icon-192x192.png',
    badge: '/mobile/static/icons/icon-192x192.png',
    data: { url: data.url || '/mobile/' },
    tag: data.tag || 'mobile-listing',
    renotify: true,
    requireInteraction: false,
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url ? e.notification.data.url : '/mobile/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.indexOf('/mobile') !== -1 && 'focus' in w) return w.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
