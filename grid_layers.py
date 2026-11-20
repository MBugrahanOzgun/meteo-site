"""Model haritası için ECMWF açık verisi (IFS HRES, 0,25°).

ECMWF'nin herkese açık gerçek zamanlı tahmin verisi (CC BY 4.0) her model
koşusunda (00 ve 12 UTC) indirilir, Türkiye ve çevresi kutusu kesilir ve
tarayıcının çizeceği küçük ikili dosyalara (int16) dönüştürülür.

  Kaynak : https://data.ecmwf.int/forecasts (ücretsiz, API anahtarı yok)
  Lisans : CC BY 4.0 — haritada "ECMWF" kaynağı gösterilir.
  Adımlar: 0–144 sa 3'er saat, 150–168 sa 6'şar saat (7 gün, 53 kare)

Her adım için sadece gereken alanlar indirilir: .index dosyasından alanın
bayt aralığı okunur, HTTP Range isteğiyle yalnızca o parça çekilir.

Diskte (DB_PATH ile aynı kalıcı disk):
  layers/<koşu>/meta.json         kutu, adımlar, ölçekler
  layers/<koşu>/<değişken>.bin    int16, [adım][satır(kuzey→güney)][sütun(batı→doğu)]
  layers/latest.json              en son tamamlanan koşunun meta'sı
"""

import fcntl
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

log = logging.getLogger("grid_layers")

ROOT = "https://data.ecmwf.int/forecasts"

# Türkiye ve çevresi (0,25° ızgara noktaları dahil)
LAT_N, LAT_S, LON_W, LON_E, RES = 44.0, 34.0, 24.0, 46.0, 0.25
NY = int(round((LAT_N - LAT_S) / RES)) + 1   # 41
NX = int(round((LON_E - LON_W) / RES)) + 1   # 89

STEPS = list(range(0, 145, 3)) + list(range(150, 169, 6))   # 53 adım, 168 sa
LAST_STEP = STEPS[-1]

# ECMWF parametresi → çıktı değişkeni
PARAMS = ["2t", "10u", "10v", "10fg", "tp", "sf", "tcc"]

# Çıktı değişkenleri: int16 ölçeği ve birimi (değer = tam sayı × ölçek)
VARS = {
    "temp":   {"scale": 0.1,  "unit": "°C"},       # 2 m sıcaklık
    "u":      {"scale": 0.1,  "unit": "m/s"},      # 10 m rüzgâr, doğu bileşeni
    "v":      {"scale": 0.1,  "unit": "m/s"},      # 10 m rüzgâr, kuzey bileşeni
    "gust":   {"scale": 0.1,  "unit": "m/s"},      # önceki adımdan bu yana en yüksek hamle
    "precip": {"scale": 0.01, "unit": "mm/h"},     # adım aralığında ortalama yağış şiddeti
    "snow":   {"scale": 0.01, "unit": "cm/h"},     # kar yağışı (su eşdeğeri mm ≈ cm kar, 10:1)
    "cloud":  {"scale": 1,    "unit": "%"},        # toplam bulut örtüsü
}
MISSING = -32768

DATA_DIR = os.path.dirname(os.path.abspath(os.environ.get("DB_PATH", "meteo.db")))
LAYERS_DIR = os.path.join(DATA_DIR, "layers")
KEEP_RUNS = 2

# /layers/status için son durum (hata ayıklama)
status = {"last_check": None, "last_result": None, "last_error": None, "processing": None, "progress": None}


# ---------------------------------------------------------------- yardımcılar
def run_id(run: datetime) -> str:
    return run.strftime("%Y%m%d%H")


def file_url(run: datetime, step: int, ext: str) -> str:
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    return f"{ROOT}/{d}/{h}z/ifs/0p25/oper/{d}{h}0000-{step}h-oper-fc.{ext}"


def candidate_runs(now: datetime) -> list[datetime]:
    """En yeni 00/12 UTC koşusundan geriye doğru 4 aday."""
    base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base = base.replace(hour=0 if base.hour < 12 else 12)
    return [base - timedelta(hours=12 * i) for i in range(4)]


def latest_meta() -> dict | None:
    try:
        with open(os.path.join(LAYERS_DIR, "latest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _get(client: httpx.Client, url: str, headers: dict | None = None, tries: int = 3) -> httpx.Response:
    last = None
    for attempt in range(tries):
        try:
            r = client.get(url, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return r
        except httpx.HTTPError as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last or RuntimeError(f"istek başarısız: {url}")


def parse_index(text: str) -> dict:
    """ECMWF .index (her satır bir JSON) → {param: (offset, length)} (sadece yüzey alanları)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("levtype") != "sfc":
            continue
        p = e.get("param")
        if p in PARAMS and p not in out:
            out[p] = (int(e["_offset"]), int(e["_length"]))
    return out


def decode_crop(msg: bytes) -> np.ndarray:
    """Tek GRIB mesajını çözüp Türkiye kutusunu (NY×NX, kuzeyden güneye) döndürür."""
    import eccodes   # ağır kütüphane; sadece işleme sırasında yüklenir

    gid = eccodes.codes_new_from_message(msg)
    try:
        g = lambda k: eccodes.codes_get(gid, k)
        if g("gridType") != "regular_ll":
            raise ValueError(f"beklenmeyen ızgara: {g('gridType')}")
        ni, nj = int(g("Ni")), int(g("Nj"))
        lat1 = float(g("latitudeOfFirstGridPointInDegrees"))
        lon1 = float(g("longitudeOfFirstGridPointInDegrees"))
        di = float(g("iDirectionIncrementInDegrees"))
        dj = float(g("jDirectionIncrementInDegrees"))
        jpos = int(g("jScansPositively"))
        ineg = int(g("iScansNegatively"))
        vals = eccodes.codes_get_values(gid).astype(np.float64)
        if int(g("bitmapPresent")):
            vals[vals == float(g("missingValue"))] = np.nan
    finally:
        eccodes.codes_release(gid)

    vals = vals.reshape(nj, ni)
    lats = LAT_N - np.arange(NY) * RES
    lons = LON_W + np.arange(NX) * RES
    rows = np.rint(((lats - lat1) if jpos else (lat1 - lats)) / dj).astype(int)
    dl = ((lons - lon1) % 360.0) if not ineg else ((lon1 - lons) % 360.0)
    cols = np.rint(dl / di).astype(int)
    if rows.min() < 0 or rows.max() >= nj or cols.min() < 0 or cols.max() >= ni:
        raise ValueError("Türkiye kutusu ızgaranın dışında kaldı")
    return vals[np.ix_(rows, cols)]


def _to_int16(a: np.ndarray, scale: float) -> np.ndarray:
    q = np.rint(a / scale)
    q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), MISSING)
    return q.astype("<i2")


# ---------------------------------------------------------------- ana iş
def find_available_run(client: httpx.Client, now: datetime) -> datetime | None:
    """Son adımı (168 sa) yayımlanmış en yeni koşu."""
    for run in candidate_runs(now):
        if now - run < timedelta(hours=6):      # ECMWF veriyi koşudan ~7-9 sa sonra yayımlar
            continue
        r = _get(client, file_url(run, LAST_STEP, "index"), tries=2)
        if r.status_code == 200 and r.text.strip():
            return run
    return None


def process_run(client: httpx.Client, run: datetime) -> dict:
    """Koşuyu indirip işler, diske yazar ve meta'yı döndürür."""
    rid = run_id(run)
    n = len(STEPS)
    out = {k: np.full((n, NY, NX), np.nan, dtype=np.float32) for k in VARS}
    prev_tp = np.zeros((NY, NX))     # birikim koşu başında sıfırdır
    prev_sf = np.zeros((NY, NX))
    prev_step = 0
    got = 0

    for si, step in enumerate(STEPS):
        status["progress"] = f"{si + 1}/{n}"
        r = _get(client, file_url(run, step, "index"))
        if r.status_code != 200:
            raise RuntimeError(f"{rid} +{step}h index alınamadı (HTTP {r.status_code})")
        idx = parse_index(r.text)
        fields = {}
        for p, (off, length) in idx.items():
            rr = _get(client, file_url(run, step, "grib2"), headers={"Range": f"bytes={off}-{off + length - 1}"})
            if rr.status_code not in (200, 206):
                raise RuntimeError(f"{rid} +{step}h {p} alınamadı (HTTP {rr.status_code})")
            body = rr.content if rr.status_code == 206 else rr.content[off:off + length]
            fields[p] = decode_crop(body)

        if "2t" in fields:
            out["temp"][si] = fields["2t"] - 273.15
        if "10u" in fields:
            out["u"][si] = fields["10u"]
        if "10v" in fields:
            out["v"][si] = fields["10v"]
        if "10fg" in fields and step > 0:
            out["gust"][si] = fields["10fg"]
        if "tcc" in fields:
            tcc = fields["tcc"]
            out["cloud"][si] = tcc * 100.0 if np.nanmax(tcc) <= 1.5 else tcc
        # tp / sf: koşu başından beri birikimli (m) → aralıktaki ortalama şiddet (mm/sa)
        hours = step - prev_step
        if step == 0:
            out["precip"][si] = 0.0
            out["snow"][si] = 0.0
        if "tp" in fields:
            tp = fields["tp"] * 1000.0
            if step > 0 and hours > 0:
                out["precip"][si] = np.clip((tp - prev_tp) / hours, 0, None)
            prev_tp = tp
        if "sf" in fields:
            sf = fields["sf"] * 1000.0     # mm su eşdeğeri ≈ cm kar (10:1)
            if step > 0 and hours > 0:
                out["snow"][si] = np.clip((sf - prev_sf) / hours, 0, None)
            prev_sf = sf
        if fields:
            got += 1
        prev_step = step

    if got < n * 0.9:
        raise RuntimeError(f"{rid}: {n} adımdan sadece {got} tanesi işlenebildi")

    # Rüzgâr ilk adımda hamle yok: bir sonraki adımı kullan
    if n > 1:
        out["gust"][0] = out["gust"][1]

    tmp = os.path.join(LAYERS_DIR, f".tmp-{rid}")
    final = os.path.join(LAYERS_DIR, rid)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for k, spec in VARS.items():
        _to_int16(out[k], spec["scale"]).tofile(os.path.join(tmp, f"{k}.bin"))

    times = [(run + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%MZ") for s in STEPS]
    meta = {
        "run_id": rid,
        "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ECMWF IFS HRES açık veri (CC BY 4.0)",
        "lat_n": LAT_N, "lat_s": LAT_S, "lon_w": LON_W, "lon_e": LON_E, "res": RES,
        "nx": NX, "ny": NY, "steps": STEPS, "times": times,
        "vars": VARS, "missing": MISSING,
    }
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)

    tmp_latest = os.path.join(LAYERS_DIR, "latest.json.tmp")
    with open(tmp_latest, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp_latest, os.path.join(LAYERS_DIR, "latest.json"))

    # eski koşuları temizle
    runs = sorted(d for d in os.listdir(LAYERS_DIR) if d.isdigit() and len(d) == 10)
    for d in runs[:-KEEP_RUNS]:
        shutil.rmtree(os.path.join(LAYERS_DIR, d), ignore_errors=True)
    return meta


def refresh(now: datetime | None = None) -> str:
    """Yeni bir koşu yayımlandıysa indirip işler. Arka plan görevinden çağrılır (bloklayan)."""
    now = now or datetime.now(timezone.utc)
    os.makedirs(LAYERS_DIR, exist_ok=True)
    status["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lock = open(os.path.join(LAYERS_DIR, ".lock"), "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "başka bir işlem sürüyor"
        with httpx.Client(timeout=60, follow_redirects=True,
                          headers={"User-Agent": "forecaster.com.tr/1.0 (model map)"}) as client:
            run = find_available_run(client, now)
            if run is None:
                status["last_result"] = "yayımlanmış koşu bulunamadı"
                return status["last_result"]
            cur = latest_meta()
            if cur and cur.get("run_id") >= run_id(run):
                status["last_result"] = f"güncel ({cur['run_id']})"
                return status["last_result"]
            status["processing"] = run_id(run)
            t0 = time.time()
            meta = process_run(client, run)
            status["last_result"] = f"{meta['run_id']} işlendi ({time.time() - t0:.0f} sn)"
            status["last_error"] = None
            log.info("model haritası: %s", status["last_result"])
            return status["last_result"]
    except Exception as e:   # arka plan görevi asla çökmesin; eski veri kalır
        status["last_error"] = f"{type(e).__name__}: {e}"
        log.exception("model haritası güncellenemedi")
        return status["last_error"]
    finally:
        status["processing"] = None
        status["progress"] = None
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()
