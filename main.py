import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# Arşiv veritabanı. DB_PATH env değişkeniyle konumu değiştirilebilir
# (ör. Render'da kalıcı disk bağlarsan /data/meteo.db yaparsın).
DB_PATH = os.environ.get("DB_PATH", "meteo.db")

# Admin paneli şifresi — Render'da ortam değişkeni olarak ayarlanır,
# koda asla gömülmez. Ayarlanmamışsa admin uçları devre dışı kalır.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

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

    # Admin panelinden girilen uzman değerlendirme paragrafları (tarihli liste)
    con.execute("""
        CREATE TABLE IF NOT EXISTS expert_notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Admin panelinden girilen excel-benzeri tablo — tek satır, JSON olarak tutulur
    # (başlık/sütun sayısı sabit olmadığı için esnek şema)
    con.execute("""
        CREATE TABLE IF NOT EXISTS expert_table (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            data_json  TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
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


# -------------------- ADMIN AUTH --------------------
class LoginPayload(BaseModel):
    password: str


class NotePayload(BaseModel):
    title: str
    body: str


class TablePayload(BaseModel):
    headers: list[str] = []
    rows: list[list[str]] = []


def check_admin(authorization: str | None = Header(default=None)):
    """Yazma uçlarını korur. 'Authorization: Bearer <ADMIN_PASSWORD>' bekler."""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Sunucuda ADMIN_PASSWORD ayarlanmamış")
    if authorization != f"Bearer {ADMIN_PASSWORD}":
        raise HTTPException(status_code=401, detail="Yetkisiz")


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


# -------------------- ADMIN PANELİ (Uzman Notu) --------------------
@app.post("/admin/login")
def admin_login(payload: LoginPayload):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Sunucuda ADMIN_PASSWORD ayarlanmamış")
    if payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Hatalı şifre")
    # Basit model: şifrenin kendisi, sonraki isteklerde token gibi kullanılır.
    return {"ok": True, "token": ADMIN_PASSWORD}


@app.get("/notes")
def get_notes():
    """Herkese açık: yayınlanmış uzman değerlendirme notları, en yeni önce."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, title, body, created_at FROM expert_notes ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return {"notes": [dict(r) for r in rows]}


@app.post("/notes")
def add_note(payload: NotePayload, authorization: str | None = Header(default=None)):
    check_admin(authorization)
    title = payload.title.strip()
    body = payload.body.strip()
    if not title or not body:
        raise HTTPException(status_code=400, detail="Başlık ve metin gerekli")
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO expert_notes (title, body, created_at) VALUES (?,?,?)",
        (title, body, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True}


@app.delete("/notes/{note_id}")
def delete_note(note_id: int, authorization: str | None = Header(default=None)):
    check_admin(authorization)
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM expert_notes WHERE id=?", (note_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/table")
def get_table():
    """Herkese açık: admin panelinden girilen excel-benzeri tablo."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT data_json, updated_at FROM expert_table WHERE id=1").fetchone()
    con.close()
    if not row:
        return {"headers": [], "rows": [], "updated_at": None}
    data = json.loads(row["data_json"])
    return {"headers": data.get("headers", []), "rows": data.get("rows", []), "updated_at": row["updated_at"]}


@app.post("/table")
def save_table(payload: TablePayload, authorization: str | None = Header(default=None)):
    check_admin(authorization)
    data_json = json.dumps({"headers": payload.headers, "rows": payload.rows}, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO expert_table (id, data_json, updated_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at""",
        (data_json, now),
    )
    con.commit()
    con.close()
    return {"ok": True}


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
                        "Yenilenebilir enerji sektörünün birincil hammaddesi meteorolojik kaynaklardır. Dolayısıyla bu sektör büyük oranda tahmin edilebilir bir özellik taşır. Tahminler alansal, noktasal, saatlik, günlük, haftalık, aylık ve mevsimlik olmak üzere hem zamansal hem alansal olarak farklı şekillerde yapılabilir. Genel hava tahmininin yanında tesisinizin mevcut istasyonu kullanılarak yapılacak değerlendirmelerle en doğru sonuçları sunabiliriz.",
                        "Son yıllarda artan enerji yatırımları üretim planlamasını da gerekli kılmıştır. İleriye yönelik üretilebilecek enerji miktarının birkaç gün öncesinden değerlendirilmesi önem arz eder. Tahmin edilen meteorolojik parametreler, enerji üretim tahmini için birincil parametredir. Yapacağımız tahminlerle hem kârlılığınızı hem üretim verimliliğinizi artırabilirsiniz."
                    ]
                },
                {
                    "h": "Yenilenebilir Enerji Tesisleri için Saha Analizi",
                    "p": [
                        "Deneyimli ekibimizle enerji piyasası yatırımlarınız için kapsamlı saha analizleri yapıyoruz. Enerji piyasası, yenilenebilir enerji, mevzuat, meteorolojik veri analizleri ve sektör hakkında bilmek istediğiniz tüm konularda kapsamlı raporlar hazırlayabiliriz.",
                        "Muhtemel sahalarınız için meteorolojik veri arşivinin temini ve analizi ile hem öngörü yapabilme imkânı hem de risk haritalarınızın oluşturulmasını sağlıyor; kurulum ve işletme aşamalarınızda strateji belirlemenize destek oluyoruz."
                    ]
                },
                {
                    "h": "Sizlere Hangi Meteorolojik Bilgileri Sunuyoruz",
                    "list": [
                        "Bölgenin iklim ve meteoroloji verileri",
                        "Tekerrür haritaları",
                        "Risk haritaları ve bilgileri",
                        "Don / kar örtüsü başlangıç ve bitiş tarihleri, belirli gün tarihleri",
                        "Yağış tür ve şiddeti için uydu ve radar görüntüleri",
                        "Yıldırım, dolu izleme ve muhtemel gelişme sahaları",
                        "Hub yüksekliği ve yer seviyesi için rüzgar, nem, sıcaklık, güneş radyasyonu ve bulutluluk çıktısı",
                        "RES ve GES için tüm parametreleri içeren sayısal model çıktıları",
                        "İlk 48 saat için saatlik hava tahmini",
                        "15 günlük hava tahmini",
                        "Sıcaklık, basınç ve yağış parametreleri için aylık tahmin",
                        "Mevsimsel değerlendirmeler"
                    ]
                },
                {
                    "h": "Danışmanlık",
                    "p": [
                        "Size özel hazırladığımız raporları seçeceğiniz iletişim araçlarıyla (e-posta, SMS, telefon vb.) sunabildiğimiz gibi, istediğiniz zaman ulaşabileceğiniz meteoroloji uzmanlarımızla mevcut durumu birlikte değerlendirebilirsiniz."
                    ]
                }
            ]
        },
        {
            "id": "marine",
            "title": "Denizcilik",
            "sections": [
                {
                    "h": "Denizde Doğru Tahmin, Güvenli Planlama",
                    "p": [
                        "Üç tarafı denizlerle çevrili ülkemizde denizcilik, önemi gün geçtikçe artan bir sektördür ve hava şartlarıyla doğrudan ilişkilidir. Uzun kıyı şeridimiz boyunca uzman tahmincilerimizle 3'er saatlik periyotlarda rüzgar yön ve hızı, dalga yüksekliği ve hava tahmini desteği sunuyoruz.",
                        "Kötü hava şartları planlarınızı tamamen aksatabilir; ancak önceden haberdar olacağınız meteorolojik şartlara göre yapacağınız planlar size kazanç, tasarruf ve daha keyifli bir seyir olarak geri döner."
                    ]
                },
                {
                    "h": "Neden Yüksek Çözünürlüklü Kıyı Tahmini?",
                    "p": [
                        "İnternet taramalarında karşınıza çıkan tahminler genellikle açık deniz tahminleridir ve çok sayıda koy barındıran ülkemiz denizlerini temsil etmeleri güçtür. Kıyılarımızı daha iyi temsil eden, daha yüksek çözünürlüklü modeller çalıştırarak ve bunları tecrübeli tahmincilerle değerlendirerek en doğru raporları hazırlıyoruz.",
                        "Tahminlerimiz içerisinde 10 güne kadar belirli periyotlarla ortalama ve maksimum rüzgar, hadise ve genel hava tahmini; 11-30 gün arasında ise genel meteorolojik durum bilgileri yer alır. Ülkemiz genelindeki marinalar için de detaylı hava tahminleri sunuyoruz."
                    ]
                },
                {
                    "h": "Danışmanlık",
                    "p": [
                        "Size özel hazırladığımız raporları seçeceğiniz iletişim araçlarıyla (e-posta, SMS, telefon vb.) sunabildiğimiz gibi, istediğiniz zaman ulaşabileceğiniz meteoroloji uzmanlarımızla mevcut durumu birlikte değerlendirebilirsiniz. Rüzgar klimatolojisi araştırma çalışmalarımız rüzgar gülü olarak sunulur."
                    ]
                }
            ]
        },
        {
            "id": "construction",
            "title": "İnşaat ve Müteahhitlik",
            "sections": [
                {
                    "h": "Şantiyede Her Gün Hava Şartlarına Bağlıdır",
                    "p": [
                        "Meteorolojik şartlar inşaat projelerinin her aşamasında belirleyici rol oynar. Büyük oranda açık havada çalışılan sektörde hem çalışma şartları hem de üretim kalitesi meteorolojik parametrelerden etkilenir. İnşaat sezonunun başlangıç ve bitiş tarihlerinde 1 haftalık bir değişikliğin dahi şantiye maliyetlerine yansıması önemlidir.",
                        "Sağlıklı ve doğru meteorolojik destekle, şantiye başlangıç ve bitiş tarihlerinin yanı sıra inşaat süresi boyunca kazanacağınız zaman sektörde öne geçmenizi sağlar. Bu verimlilik artışı, planlı ve kazançlı işgücü ile işyeri güvenliğinde etkinlik olarak geri döner."
                    ]
                },
                {
                    "h": "Hangi Parametreler, Hangi Vadede?",
                    "p": [
                        "Sıcaklık, yağış, rüzgar tahminleri ve özel projeler için basınç parametresi, projelerinizin sağlıklı devamlılığı ve kârlılığı açısından kritik bilgiler sunar. Bu bilgileri, birlikte belirleyeceğimiz eşik değerlerini de değerlendirerek en fazla fayda sağlayacak formatta ve zamanda kullanımınıza sunuyoruz.",
                        "Çok kısa vadeli tahminlerle gününüzü planlayabilir; kısa, orta ve uzun vadeli tahminlerle aylık iş takviminizi oluşturabilirsiniz. İsteğiniz doğrultusunda hazırlayacağımız geriye dönük klimatolojik çalışmalarla sahalarınızın risk dönemlerini ve önemli meteorolojik sınır değerlerini belirleyebiliriz."
                    ]
                }
            ]
        },
        {
            "id": "agriculture",
            "title": "Tarım",
            "sections": [
                {
                    "h": "Ekimden Hasada, Depodan Pazara",
                    "p": [
                        "Zirai üretim faaliyetleri ister açık alanda ister sera gibi kapalı alanlarda yapılsın hava durumuyla doğrudan ilişkilidir. Tarımsal faaliyetlerin her aşamasını etkileyen meteorolojik parametreler tahmin edilebilir sınıftadır. Bu parametreleri hizmetinize sunarak birkaç gün öncesinden önlem almanızı veya ticari şartlarınızı planlamanızı sağlıyoruz.",
                        "Havanın etkisini ekimden hasada, hatta depolama ve pazarlamaya kadar her alanda görebiliriz. Zirai don, kuraklık, yağış ve sıcaklık gibi parametrelerin tahmini; üretim miktarı ve kalitesi kadar pazar şartlarının nasıl olabileceğine dair de ipuçları verir.",
                        "Seracılık, hayvancılık ve özel üretim çiftlikleriniz için ısıtma-soğutma faaliyetlerinizde kullanılmak üzere hassas noktasal tahminler yaparak diğer üreticilerin önüne geçmenizi sağlayabiliriz."
                    ]
                },
                {
                    "h": "Sunduğumuz Tahmin Ürünleri",
                    "list": [
                        "Günlük, 15 günlük, 3 aylık ve mevsimlik tahminler",
                        "15 günlük tahmin: min/maks sıcaklık, yağış miktarı ve cinsi, rüzgar hızı, nem, zirai don",
                        "3 aylık tahmin: aylık ortalama sıcaklık, toplam yağış ve normallere göre beklenti",
                        "Mevsimlik değerlendirme: sistem konumları ve muhtemel riskler (kuraklık, ani soğuma/ısınma, fazla yağış)",
                        "İlk 10 günlük dönem için kuvvetli hadiselere yönelik risk haritaları",
                        "Tüm il ve ilçe merkezleri ile verilecek koordinatlara göre noktasal tahminler",
                        "Faaliyetiniz özelinde meteorolojik bilgilerin yorumlanması"
                    ]
                },
                {
                    "h": "Danışmanlık",
                    "p": [
                        "Size özel hazırladığımız raporları seçeceğiniz iletişim araçlarıyla (e-posta, SMS, telefon vb.) sunabildiğimiz gibi, istediğiniz zaman ulaşabileceğiniz meteoroloji uzmanlarımızla mevcut durumu birlikte değerlendirebilirsiniz."
                    ]
                }
            ]
        }
    ]
}


@app.get("/content")
def content():
    return CONTENT
