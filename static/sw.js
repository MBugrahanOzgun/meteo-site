/* Forecaster service worker
   Sitenin telefona "uygulama" olarak yüklenebilmesi için gerekli.
   Bilerek HİÇBİR ŞEYİ önbelleğe almaz: hava verisi canlı olduğu için
   her istek doğrudan internete gider, kullanıcı asla eski bir sürümde
   takılı kalmaz. Tek işi, internet yokken sayfa açılmaya çalışılırsa
   boş/hata ekranı yerine "bağlantı yok" sayfası göstermektir.
   /sw.js adresinden servis edilir (main.py), böylece tüm siteyi kapsar. */

const OFFLINE_HTML = `<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#061219">
<title>Bağlantı yok · Forecaster</title>
<style>
  html,body{height:100%;margin:0}
  body{background:#061219;color:#e0f2fe;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
       display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;box-sizing:border-box}
  h1{font-size:22px;margin:20px 0 8px}
  p{color:#94a3b8;font-size:15px;line-height:1.5;margin:0 0 24px;max-width:320px}
  button{background:linear-gradient(135deg,#14b8a6,#0f766e);color:#fff;border:0;border-radius:10px;
         padding:12px 26px;font-size:15px;font-weight:600;cursor:pointer}
</style></head><body><div>
<svg width="72" height="72" viewBox="0 0 100 100" fill="none" stroke="#7eead4" stroke-linecap="round">
  <circle cx="50" cy="50" r="37" stroke-width="3.2"/>
  <path d="M 24,42 C 29,33 35,33 40,42 C 45,51 51,51 56,42 C 61,33 67,33 72,42" stroke-width="3"/>
  <path d="M 24,58 C 29,49 35,49 40,58 C 45,67 51,67 56,58 C 61,49 67,49 72,58" stroke-width="3"/>
</svg>
<h1>İnternet bağlantısı yok</h1>
<p>Forecaster canlı hava verisi kullanıyor. Bağlantınız geldiğinde yeniden deneyin.</p>
<button onclick="location.reload()">Yeniden dene</button>
</div></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  const req = event.request;
  /* Sadece sayfa açılışlarına karışılır; veri, harita, resim vb. isteklere dokunulmaz */
  if (req.mode !== 'navigate' || req.method !== 'GET') return;
  event.respondWith(
    fetch(req).catch(() => new Response(OFFLINE_HTML, {
      status: 200,
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
    }))
  );
});
