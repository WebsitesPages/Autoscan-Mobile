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

// Push-Abo wurde von der Plattform rotiert/erneuert (passiert auf iOS alle paar Tage).
// Ohne diesen Handler bliebe das neue Abo dem Server unbekannt → keine Notifications mehr.
function urlB64ToUint8(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

self.addEventListener('pushsubscriptionchange', (e) => {
  e.waitUntil((async () => {
    try {
      const res = await fetch('/mobile/api/push/vapid_public');
      const data = await res.json();
      if (!data || !data.key) return;
      const newSub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8(data.key)
      });
      const oldEp = (e.oldSubscription && e.oldSubscription.endpoint) || null;
      await fetch('/mobile/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // filters bewusst weggelassen → Server übernimmt sie vom alten Abo
        body: JSON.stringify({ subscription: newSub.toJSON(), old_endpoint: oldEp })
      });
    } catch (err) { /* ignore */ }
  })());
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
