import os
import sqlite3
from contextlib import asynccontextmanager

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# Arşiv veritabanı. DB_PATH env değişkeniyle konumu değiştirilebilir
# (ör. Render'da kalıcı disk bağlarsan /data/meteo.db yaparsın).
DB_PATH = os.environ.get("DB_PATH", "meteo.db")

# 10 dk cache: Open-Meteo + Nominatim tekrar çağrılarını azaltır
cache = TTLCache(maxsize=512, ttl=600)


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS weather_archive (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at   TEXT NOT NULL,      -- Open-Meteo gözlem zamanı (ISO)
            lat           REAL NOT NULL,
            lon           REAL NOT NULL,
            temperature_c REAL,
            humidity_pct  REAL,
            pressure_hpa  REAL,
            wind_speed_ms REAL,
            wind_dir_deg  REAL,
            UNIQUE(recorded_at, lat, lon)     -- aynı an/konum iki kez yazılmaz
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_arch_loc ON weather_archive(lat, lon, recorded_at)")
    con.commit()
    con.close()


def archive_snapshot(lat: float, lon: float, current: dict):
    """Taze çekilen anlık veriyi arşive yazar (aynı kayıt varsa sessizce geçer)."""
    if not current.get("time"):
        return
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            """INSERT OR IGNORE INTO weather_archive
               (recorded_at, lat, lon, temperature_c, humidity_pct,
                pressure_hpa, wind_speed_ms, wind_dir_deg)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                current.get("time"), round(lat, 4), round(lon, 4),
                current.get("temperature_2m"), current.get("relative_humidity_2m"),
                current.get("pressure_msl"), current.get("wind_speed_10m"),
                current.get("wind_direction_10m"),
            ),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        pass  # arşiv yazılamazsa site çalışmaya devam etsin


# -------------------- LIFESPAN --------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.http = httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "meteo-site/1.0 (local dev)"},
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="Meteoroloji Dashboard API", lifespan=lifespan)

# Büyük JSON cevaplarını (hourly, content) sıkıştırır
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Static dosyalar (HTML, js, css)
app.mount("/static", StaticFiles(directory="static"), name="static")

# NOT: CORS middleware kaldırıldı. Frontend'i FastAPI'nin kendisi servis
# ettiği için (aynı origin) CORS'a gerek yok. Ayrı bir dev server (Vite vb.)
# kullanacaksan CORSMiddleware'i geri ekle.


# -------------------- HELPERS --------------------
async def get_open_meteo(lat: float, lon: float) -> dict:
    """Open-Meteo ham cevabını tek anahtarla cache'ler.

    /latest ve /hourly aynı ham veriyi paylaşır -> API çağrısı yarıya iner.
    """
    key = ("open-meteo", round(lat, 4), round(lon, 4))
    if key in cache:
        return cache[key]

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m",
        "hourly": "temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m",
        "timezone": "auto",
    }
    try:
        r = await app.state.http.get(OPEN_METEO_URL, params=params)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo erişilemedi: {e}")

    data = r.json()
    cache[key] = data
    # Taze veri geldi -> arşive işle
    archive_snapshot(lat, lon, data.get("current", {}) or {})
    return data


async def fetch_place_name(lat: float, lon: float) -> dict:
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lon,
        "zoom": 10,
        "addressdetails": 1,
    }
    try:
        r = await app.state.http.get(NOMINATIM_REVERSE_URL, params=params)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Nominatim erişilemedi: {e}")

    j = r.json()
    addr = j.get("address", {}) or {}
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
    district = addr.get("suburb") or addr.get("city_district") or addr.get("district")

    return {
        "display_name": j.get("display_name"),
        "city": city,
        "district": district,
        "state": addr.get("state"),
        "country": addr.get("country"),
    }


# -------------------- ROUTES --------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "meteo-site"}


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/latest")
async def latest(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    data = await get_open_meteo(lat, lon)
    c = data.get("current", {}) or {}
    return {
        "location": {"lat": lat, "lon": lon, "timezone": data.get("timezone")},
        "current": {
            "time": c.get("time"),
            "temperature_c": c.get("temperature_2m"),
            "humidity_pct": c.get("relative_humidity_2m"),
            "pressure_hpa": c.get("pressure_msl"),
            "wind_speed_ms": c.get("wind_speed_10m"),
            "wind_dir_deg": c.get("wind_direction_10m"),
        },
    }


@app.get("/hourly")
async def hourly(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    data = await get_open_meteo(lat, lon)
    h = data.get("hourly", {}) or {}
    return {
        "location": {"lat": lat, "lon": lon, "timezone": data.get("timezone")},
        "hourly": {
            "time": h.get("time", []),
            "temperature_c": h.get("temperature_2m", []),
            "humidity_pct": h.get("relative_humidity_2m", []),
            "pressure_hpa": h.get("pressure_msl", []),
            "wind_speed_ms": h.get("wind_speed_10m", []),
        },
    }


@app.get("/place")
async def place(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    key = ("place", round(lat, 4), round(lon, 4))
    if key in cache:
        return cache[key]

    result = await fetch_place_name(lat, lon)
    cache[key] = result
    return result


# -------------------- ARCHIVE --------------------
@app.get("/archive")
def archive(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    days: int = Query(7, ge=1, le=365),
):
    """Bir konumun arşivlenmiş geçmiş kayıtları (son N gün)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT recorded_at, temperature_c, humidity_pct,
                  pressure_hpa, wind_speed_ms, wind_dir_deg
           FROM weather_archive
           WHERE lat = ? AND lon = ?
             AND recorded_at >= datetime('now', ?)
           ORDER BY recorded_at""",
        (round(lat, 4), round(lon, 4), f"-{days} days"),
    ).fetchall()
    con.close()
    return {
        "location": {"lat": lat, "lon": lon},
        "days": days,
        "count": len(rows),
        "records": [dict(r) for r in rows],
    }


@app.get("/archive/locations")
def archive_locations():
    """Arşivde kaydı bulunan konumlar ve kayıt sayıları."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT lat, lon, COUNT(*) AS records,
                  MIN(recorded_at) AS first_record,
                  MAX(recorded_at) AS last_record
           FROM weather_archive
           GROUP BY lat, lon
           ORDER BY records DESC"""
    ).fetchall()
    con.close()
    return {"locations": [dict(r) for r in rows]}


# -------------------- CONTENT (Tabs) --------------------
# Statik içerik: her istekte yeniden oluşturmaya ve cache'lemeye gerek yok,
# modül seviyesinde bir kez tanımlanır.
CONTENT = {
    "tabs": [
        {
            "id": "energy",
            "title": "Yenilenebilir Enerji",
            "sections": [
                {
                    "h": "Enerji Sektöründe Hava Tahmininin Önemi",
                    "p": [
                        "Yenilenebilir enerji sektörünün karakteristik olarak birincil hammaddesi meteorolojik kaynaklardır. Meteoroloji parametrelerinin alansal, noktasal, saatlik, günlük, haftalık, aylık ve mevsimlik tahminleri yapılabilir.",
                        "Artan enerji yatırımları üretim planlamasını gerekli kılmıştır. Önümüzdeki günlerde üretilebilecek enerji miktarını önceden değerlendirmek büyük önem taşır. Tahmin edilen meteorolojik parametreler enerji üretim tahmini için birincil parametredir."
                    ]
                },
                {
                    "h": "Yenilenebilir Enerji Tesisleri için Saha Analizi",
                    "p": [
                        "Deneyimli ekip ile yatırım yapılması planlanan sahalar için kapsamlı saha analizi yapılabilir. Meteorolojik veri arşivinin temini, analizi, öngörü imkanı ve risk haritalarının oluşturulması ile kurulum ve işletme stratejileri belirlenebilir.",
                        "Tesisin sağlıklı kurulumu, işletmesi ve en verimli çalışması için gerekli meteorolojik veri analizleri değerlendirilerek raporlanır."
                    ]
                },
                {
                    "h": "Sizlere Hangi Meteorolojik Bilgileri Sunuyoruz",
                    "list": [
                        "Bölgenin iklim ve meteorolojik verileri",
                        "Tekerrür haritaları",
                        "Risk haritaları ve bilgileri",
                        "Don / kar örtüsü başlangıç-bitiş ve belirli gün tarihleri",
                        "Yağış tür ve şiddeti için uydu ve radar görüntüleri",
                        "Yıldırım / dolu izleme ve muhtemel gelişme sahaları",
                        "Hub yüksekliği için rüzgar tahminleri",
                        "Yer seviyesi rüzgar, nem, sıcaklık ve bulut örtüsü tahmini",
                        "İlk 48 saat için saatlik tahmin, 7 günlük tahmin",
                        "Aylık ve mevsimlik değerlendirmeler"
                    ]
                },
                {
                    "h": "Danışmanlık",
                    "p": [
                        "Raporlar e-mail, SMS, telefon vb. iletişim araçları ile sunulabilir; ayrıca meteoroloji uzmanları ile mevcut durum değerlendirmesi yapılabilir."
                    ]
                }
            ]
        },
        {
            "id": "marine",
            "title": "Denizcilik",
            "sections": [
                {
                    "p": [
                        "Denizcilik hava şartları ile doğrudan ilişkilidir. Kıyı tahminleri, kara ile etkileşimin fazla olduğu bölgelerde açık deniz tahminlerinden daha kritik olabilir.",
                        "3 saatlik periyotlarda rüzgar yön-hız, dalga yüksekliği ve genel hava tahmini ile destek sağlanabilir. Marinalar ve balıkçı tekneleri için önemli çıktılar verir.",
                        "10 güne kadar periyotlarda ortalama/maksimum rüzgar ve yönü, hadise ve genel hava tahmini; 11–30 gün arası sinoptik değerlendirmeler sunulabilir."
                    ]
                },
                {
                    "h": "Danışmanlık",
                    "p": [
                        "Raporlar e-mail, SMS, telefon vb. iletişim araçları ile sunulabilir; meteoroloji uzmanları ile 7/24 değerlendirme yapılabilir."
                    ]
                }
            ]
        },
        {
            "id": "road",
            "title": "Karayolları",
            "sections": [
                {
                    "p": [
                        "Kamu güvenliği ve bakım faaliyetlerinin optimizasyonu için doğru ve ayrıntılı hava tahminleri temel önem taşır.",
                        "Kışın doğru tahminler gereksiz ve maliyetli tuzlama çalışmalarını azaltır. Güvenilir uyarılar zaman ve para israfını önler."
                    ]
                }
            ]
        },
        {
            "id": "aviation",
            "title": "Havacılık",
            "sections": [
                {
                    "p": [
                        "Havayolları ve havaalanları için doğru ve ayrıntılı hava tahminleri operasyonel verimlilik ve güvenlik açısından kritik öneme sahiptir.",
                        "Buzlanma, pist sıcaklığı, rota/varış meteorolojisi, türbülans haritaları gibi kapsamlı bilgiler sağlanabilir."
                    ]
                }
            ]
        },
    ]
}


@app.get("/content")
def content():
    return CONTENT
