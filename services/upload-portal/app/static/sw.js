const CACHE_NAME = "mirai-upload-pwa-v8";
const CORE_ASSETS = [
  "/static/icons/pwa-icon-180.png",
  "/static/icons/pwa-icon-192.png",
  "/static/icons/pwa-icon-512.png",
  "/static/icons/pwa-icon.svg",
];
const SHARED_CACHE = "mirai-upload-shared-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS)).catch(() => undefined),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const reqUrl = new URL(event.request.url);

  if (event.request.method === "POST" && reqUrl.pathname.startsWith("/share-target/")) {
    event.respondWith(handleShareTarget(event.request, reqUrl));
    return;
  }

  if (event.request.method !== "GET") return;

  if (reqUrl.pathname.startsWith("/__shared/")) {
    event.respondWith(serveSharedFile(event.request));
    return;
  }

  // Pages HTML (navigate / document) : network-first pour récupérer les
  // mises à jour de template immédiatement (sinon un déploiement reste
  // invisible jusqu'à expiration du cache). On retombe sur le cache si
  // offline.
  const isHtml =
    event.request.mode === "navigate" ||
    event.request.destination === "document" ||
    (event.request.headers.get("accept") || "").includes("text/html");

  if (isHtml) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200 && response.type === "basic") {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => undefined);
          }
          return response;
        })
        .catch(() => caches.match(event.request)),
    );
    return;
  }

  // Autres assets (icônes, CSS, JS) : cache-first (rapide, stable).
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          if (!response || response.status !== 200 || response.type !== "basic") {
            return response;
          }
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => undefined);
          return response;
        })
        .catch(() => cached);
    }),
  );
});

async function handleShareTarget(request, reqUrl) {
  try {
    const formData = await request.formData();
    const entries = formData.getAll("shared_audio").filter((v) => v instanceof File);
    if (!entries.length) {
      return Response.redirect(`${reqUrl.pathname.replace("/share-target/", "/upload/")}?shared_manual=1`, 303);
    }

    const cache = await caches.open(SHARED_CACHE);
    const ids = [];
    for (const f of entries) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      ids.push(id);
      const key = new Request(`/__shared/${id}`);
      const headers = new Headers({
        "Content-Type": f.type || "application/octet-stream",
        "X-Shared-Filename": f.name || `shared-${id}`,
      });
      await cache.put(key, new Response(f, { headers }));
    }

    const qrToken = reqUrl.pathname.replace("/share-target/", "");
    const redirectUrl = `/upload/${encodeURIComponent(qrToken)}?share_ids=${encodeURIComponent(ids.join(","))}&share=1`;
    return Response.redirect(redirectUrl, 303);
  } catch (_) {
    return Response.redirect(`${reqUrl.pathname.replace("/share-target/", "/upload/")}?shared_manual=1`, 303);
  }
}

async function serveSharedFile(request) {
  const cache = await caches.open(SHARED_CACHE);
  const match = await cache.match(request);
  if (!match) {
    return new Response("Not found", { status: 404 });
  }
  return match;
}
