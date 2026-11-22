"""Model haritası: ücretsiz/açık sayısal hava tahmini modelleri.

Her model koşusu yayımlandığında sunucu gereken alanları indirir, ortak
ızgaralara keser ve tarayıcının çizeceği küçük ikili dosyalara (int16)
dönüştürür.

  Model           Kaynak (ücretsiz, anahtar yok)                 Lisans
  ECMWF IFS       data.ecmwf.int  (.index + HTTP Range)           CC BY 4.0
  ECMWF AIFS      data.ecmwf.int  (.index + HTTP Range)           CC BY 4.0
  NOAA GFS        noaa-gfs-bdp-pds (AWS)  (.idx + HTTP Range)      Kamu malı
  DWD ICON-EU     opendata.dwd.de  (değişken başına .bz2 dosya)    DWD açık veri (kaynak gösterilerek)

İki ızgara kullanılır:
  tr  : Türkiye ve çevresi, 0,25°  — yüzey alanları (yağış, sıcaklık, rüzgâr …)
  syn : Avrupa–Orta Doğu, 0,5°     — sinoptik alanlar (basınç, 500/850 hPa)

Birikimli alanlar (yağış, kar) her GRIB mesajının kendi başlangıç/bitiş
adımına göre şiddete çevrilir; böylece "koşu başından beri" (ECMWF, ICON),
"6 saatlik kova" (GFS) ve aralık birikimi (AIFS) aynı kodla doğru işlenir.
Birimler GRIB'in kendisinden okunur.

Diskte (DB_PATH ile aynı kalıcı disk):
  layers/<model>/<koşu>/meta.json, <değişken>.bin   int16 [adım][satır K→G][sütun B→D]
  layers/<model>/latest.json                         en son tamamlanan koşunun meta'sı
"""

import bz2
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


# ================================================================ ızgaralar ve değişkenler
def _grid(lat_n, lat_s, lon_w, lon_e, res):
    return {"lat_n": lat_n, "lat_s": lat_s, "lon_w": lon_w, "lon_e": lon_e, "res": res,
            "ny": int(round((lat_n - lat_s) / res)) + 1, "nx": int(round((lon_e - lon_w) / res)) + 1}


GRIDS = {
    "tr":  _grid(44.0, 34.0, 24.0, 46.0, 0.25),   # 41 × 89
    "syn": _grid(52.0, 30.0, 10.0, 52.0, 0.5),    # 45 × 85
}
# Geriye dönük uyumluluk (ilk sürüm tek ızgaraydı)
LAT_N, LAT_S, LON_W, LON_E, RES = (GRIDS["tr"][k] for k in ("lat_n", "lat_s", "lon_w", "lon_e", "res"))
NY, NX = GRIDS["tr"]["ny"], GRIDS["tr"]["nx"]

# Çıktı değişkenleri: int16 ölçeği, birimi ve ızgarası (değer = tam sayı × ölçek)
VARS = {
    "temp":      {"scale": 0.1,  "unit": "°C",   "grid": "tr"},   # 2 m sıcaklık
    "u":         {"scale": 0.1,  "unit": "m/s",  "grid": "tr"},   # 10 m rüzgâr, doğu bileşeni
    "v":         {"scale": 0.1,  "unit": "m/s",  "grid": "tr"},   # 10 m rüzgâr, kuzey bileşeni
    "gust":      {"scale": 0.1,  "unit": "m/s",  "grid": "tr"},   # hamle
    "precip":    {"scale": 0.01, "unit": "mm/h", "grid": "tr"},   # adım aralığında ortalama yağış şiddeti
    "snow":      {"scale": 0.01, "unit": "cm/h", "grid": "tr"},   # kar yağışı (su eşdeğeri mm ≈ cm kar, 10:1)
    "cloud":     {"scale": 1,    "unit": "%",    "grid": "tr"},   # toplam bulut örtüsü
    "u100":      {"scale": 0.1,  "unit": "m/s",  "grid": "tr"},   # 100 m rüzgâr (RES göbek yüksekliği)
    "v100":      {"scale": 0.1,  "unit": "m/s",  "grid": "tr"},
    "cape":      {"scale": 1,    "unit": "J/kg", "grid": "tr"},   # konvektif kararsızlık enerjisi
    "snowdepth": {"scale": 0.1,  "unit": "cm",   "grid": "tr"},   # yerdeki kar kalınlığı
    "msl":       {"scale": 0.1,  "unit": "hPa",  "grid": "syn"},  # deniz seviyesi basıncı
    "z500":      {"scale": 1,    "unit": "m",    "grid": "syn"},  # 500 hPa jeopotansiyel yükseklik
    "t850":      {"scale": 0.1,  "unit": "°C",   "grid": "syn"},  # 850 hPa sıcaklık
}
# İndirilen ham alan (kanonik ad) → ızgara
KEY_GRID = {"t2": "tr", "u10": "tr", "v10": "tr", "gust": "tr", "cloud": "tr", "precip": "tr", "snow": "tr",
            "snowfrac": "tr", "u100": "tr", "v100": "tr", "cape": "tr", "snowdepth": "tr",
            "msl": "syn", "z500": "syn", "t850": "syn"}
MISSING = -32768

DATA_DIR = os.path.dirname(os.path.abspath(os.environ.get("DB_PATH", "meteo.db")))
LAYERS_DIR = os.path.join(DATA_DIR, "layers")
KEEP_RUNS = 2
UA = {"User-Agent": "forecaster.com.tr/1.0 (model map)"}
G0 = 9.80665

STEPS_3H_7D = list(range(0, 145, 3)) + list(range(150, 169, 6))   # 53 adım, 168 sa
STEPS_6H_7D = list(range(0, 169, 6))                              # 29 adım
STEPS_3H_5D = list(range(0, 121, 3))                              # 41 adım

# /layers/status için son durum (hata ayıklama), model başına. İşleme ayrı bir
# süreçte (layer_worker.py) çalıştığı için durum ayrıca layers/<model>/status.json
# dosyasına yazılır; web sunucusu oradan okur.
status: dict[str, dict] = {}


def _status_path(key: str) -> str:
    return os.path.join(LAYERS_DIR, key, "status.json")


def save_status(key: str, st: dict | None = None) -> None:
    st = status.get(key) if st is None else st
    if st is None:
        return
    try:
        os.makedirs(os.path.dirname(_status_path(key)), exist_ok=True)
        tmp = _status_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.replace(tmp, _status_path(key))
    except OSError:
        pass


def read_status(key: str) -> dict:
    try:
        with open(_status_path(key), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return dict(status.get(key, {}))


def mark_failed(key: str, msg: str) -> None:
    """İşleyici süreç beklenmedik biçimde bittiyse (bellek, çökme, zaman aşımı) durumu düzelt."""
    st = read_status(key)
    st.update({"last_error": msg, "processing": None, "progress": None,
               "last_check": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    status[key] = st
    save_status(key, st)


# ================================================================ yardımcılar
def run_id(run: datetime) -> str:
    return run.strftime("%Y%m%d%H")


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


def _exists(client: httpx.Client, url: str) -> bool:
    try:
        r = client.head(url)
        if r.status_code == 200:
            return True
        if r.status_code in (403, 405):            # HEAD desteklenmiyorsa 1 baytlık GET
            r = client.get(url, headers={"Range": "bytes=0-0"})
            return r.status_code in (200, 206)
    except httpx.HTTPError:
        pass
    return False


def _range(client: httpx.Client, url: str, off: int, end: int | None) -> bytes:
    rng = f"bytes={off}-{'' if end is None else end}"
    r = _get(client, url, headers={"Range": rng})
    if r.status_code == 206:
        return r.content
    if r.status_code == 200:                       # sunucu Range'i yok saydıysa
        return r.content[off:None if end is None else end + 1]
    raise RuntimeError(f"HTTP {r.status_code}: {url}")


def decode_msg(msg: bytes, grid: str = "tr") -> tuple[np.ndarray, dict]:
    """Tek GRIB mesajını çözer; istenen ızgaraya (kuzeyden güneye) keser ve
    birim / adım bilgisini döndürür."""
    import eccodes   # ağır kütüphane; sadece işleme sırasında yüklenir

    G = GRIDS[grid]
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
        info = {"units": "", "start": None, "end": None}
        try:
            info["units"] = str(g("units"))
        except Exception:
            pass
        try:
            eccodes.codes_set(gid, "stepUnits", "h")
        except Exception:
            pass
        try:
            info["start"], info["end"] = int(g("startStep")), int(g("endStep"))
        except Exception:
            pass
        vals = eccodes.codes_get_values(gid).astype(np.float64, copy=False)   # küresel alan: gereksiz kopya yok
        miss = float(g("missingValue")) if int(g("bitmapPresent")) else None
    finally:
        eccodes.codes_release(gid)

    vals = vals.reshape(nj, ni)
    lats = G["lat_n"] - np.arange(G["ny"]) * G["res"]
    lons = G["lon_w"] + np.arange(G["nx"]) * G["res"]
    rows = np.rint(((lats - lat1) if jpos else (lat1 - lats)) / dj).astype(int)
    dl = ((lons - lon1) % 360.0) if not ineg else ((lon1 - lons) % 360.0)
    cols = np.rint(dl / di).astype(int)
    if rows.min() < 0 or rows.max() >= nj or cols.min() < 0 or cols.max() >= ni:
        raise ValueError(f"{grid} ızgarası modelin alanının dışında kaldı")
    crop = vals[np.ix_(rows, cols)]              # küçük kopya; büyük dizi hemen serbest kalır
    del vals
    if miss is not None:
        crop[crop == miss] = np.nan
    return crop, info


def decode_crop(msg: bytes) -> np.ndarray:
    return decode_msg(msg)[0]


def _units(info: dict) -> str:
    return (info.get("units") or "").strip().lower()


def _to_mm(arr: np.ndarray, info: dict) -> np.ndarray:
    """Yağış/kar birikimini mm'ye (kg/m²) çevirir: ECMWF IFS metre, diğerleri kg/m²."""
    return arr * 1000.0 if _units(info) in ("m", "m of water equivalent") else arr


def _snow_cm(arr: np.ndarray, info: dict) -> np.ndarray:
    """Kar kalınlığı → cm. Gerçek kalınlık (m) doğrudan; su eşdeğeri verilmişse
    300 kg/m³ yoğunluk varsayılır (oturmuş kar)."""
    u = _units(info)
    if "water" in u or u.startswith("kg"):
        mm = arr * 1000.0 if u.startswith("m") else arr
        return mm / 300.0 * 100.0
    return arr * 100.0


def _height_m(arr: np.ndarray, info: dict) -> np.ndarray:
    """Jeopotansiyel (m²/s²) veya jeopotansiyel yükseklik (gpm) → m."""
    return arr / G0 if "s**-2" in _units(info) or "s-2" in _units(info) else arr


def _kelvin_c(arr: np.ndarray, info: dict) -> np.ndarray:
    return arr - 273.15 if _units(info) == "k" or np.nanmean(arr) > 150 else arr


def _to_int16(a: np.ndarray, scale: float) -> np.ndarray:
    q = np.rint(a / scale)
    q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), MISSING)
    return q.astype("<i2")


class Accum:
    """Birikimli alanlardan (start, end, mm) adım aralığının ortalama şiddetini (mm/sa) çıkarır."""

    def __init__(self):
        self.store: dict[tuple[int, int], np.ndarray] = {}
        self.prev = 0

    def rate(self, cands: list[tuple[int, int, np.ndarray]], step: int) -> np.ndarray | None:
        for s, e, a in cands:
            self.store[(s, e)] = a
        pe, hours, res = self.prev, step - self.prev, None
        if hours > 0:
            for s, e, a in cands:
                if e != step:
                    continue
                if s == pe:                                   # tam bu aralığın birikimi
                    res = a / hours
                    break
                if s < pe and (s, pe) in self.store:          # aynı başlangıçtan önceki birikimi çıkar
                    res = (a - self.store[(s, pe)]) / hours
                    break
        self.prev = step
        return None if res is None else np.clip(res, 0, None)


# ================================================================ modeller
class Model:
    key = label = source = note = ""
    runs: list[int] = [0, 12]
    steps: list[int] = STEPS_3H_7D
    min_lag_h = 3

    def candidate_runs(self, now: datetime) -> list[datetime]:
        base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        out, t = [], base
        while len(out) < 4:
            if t.hour in self.runs and now - t >= timedelta(hours=self.min_lag_h):
                out.append(t)
            t -= timedelta(hours=1)
        return out

    def available(self, client, run) -> bool:
        raise NotImplementedError

    def fetch_step(self, client, run, step) -> dict[str, list[bytes]]:
        """Bir adımın ham GRIB mesajları: {kanonik ad: [mesaj, …]} (ad listesi için KEY_GRID)."""
        raise NotImplementedError


def parse_ecmwf_index(text: str, params: dict) -> list[tuple[str, int, int]]:
    """ECMWF .index (her satır bir JSON) → [(kanonik ad, ofset, uzunluk)].
    params anahtarları: "2t" gibi yüzey alanı ya da ("gh", "500") gibi (alan, seviye)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p, lt = e.get("param"), e.get("levtype")
        if lt == "sfc" and p in params:
            key = params[p]
        elif lt == "pl" and (p, str(e.get("levelist"))) in params:
            key = params[(p, str(e.get("levelist")))]
        else:
            continue
        out.append((key, int(e["_offset"]), int(e["_length"])))
    return out


class EcmwfOpenData(Model):
    ROOT = "https://data.ecmwf.int/forecasts"
    PARAMS = {"2t": "t2", "10u": "u10", "10v": "v10", "10fg": "gust", "tp": "precip", "sf": "snow", "tcc": "cloud",
              "100u": "u100", "100v": "v100", "msl": "msl", "mucape": "cape", "sd": "snowdepth",
              ("gh", "500"): "z500", ("z", "500"): "z500", ("t", "850"): "t850"}

    def __init__(self, key, label, path, runs, steps, min_lag_h, source, note):
        self.key, self.label, self.path, self.runs, self.steps = key, label, path, runs, steps
        self.min_lag_h, self.source, self.note = min_lag_h, source, note

    def url(self, run, step, ext):
        d, h = run.strftime("%Y%m%d"), run.strftime("%H")
        return f"{self.ROOT}/{d}/{h}z/{self.path}/0p25/oper/{d}{h}0000-{step}h-oper-fc.{ext}"

    def available(self, client, run):
        r = _get(client, self.url(run, self.steps[-1], "index"), tries=2)
        return r.status_code == 200 and bool(r.text.strip())

    def fetch_step(self, client, run, step):
        r = _get(client, self.url(run, step, "index"))
        if r.status_code == 404:
            return {}
        if r.status_code != 200:
            raise RuntimeError(f"{self.key} {run_id(run)} +{step}h index: HTTP {r.status_code}")
        out: dict[str, list] = {}
        seen = set()
        for key, off, length in parse_ecmwf_index(r.text, self.PARAMS):
            if key in seen and key not in ("precip", "snow"):
                continue
            seen.add(key)
            out.setdefault(key, []).append(_range(client, self.url(run, step, "grib2"), off, off + length - 1))
        return out


def parse_wgrib_idx(text: str, want: dict, multi: set) -> list[tuple[str, tuple[int, int | None]]]:
    """NCEP .idx ("n:bayt:d=..:DEĞİŞKEN:SEVİYE:zaman:") → [(kanonik ad, (başlangıç, bitiş))]."""
    rows = []
    for line in text.splitlines():
        p = line.split(":")
        if len(p) >= 6 and p[1].isdigit():
            rows.append((int(p[1]), p[3], p[4], p[5]))
    out, seen = [], set()
    for i, (off, var, lev, ftime) in enumerate(rows):
        key = want.get((var, lev))
        if not key or (key in seen and key not in multi):
            continue
        if key == "snowfrac" and "ave" not in ftime and any(r[1] == var and r[2] == lev and "ave" in r[3] for r in rows):
            continue                                       # CSNOW: ortalama olanı tercih et
        end = rows[i + 1][0] - 1 if i + 1 < len(rows) else None
        out.append((key, (off, end)))
        seen.add(key)
    return out


class Gfs(Model):
    key, label = "gfs", "NOAA GFS"
    runs, steps, min_lag_h = [0, 12], STEPS_3H_7D, 3
    source = "NOAA/NCEP GFS 0,25° (kamu malı)"
    note = "NOAA GFS 0,25° (~25 km) · 3 saatlik adımlar (6. günden sonra 6 saatlik)"
    ROOTS = ["https://noaa-gfs-bdp-pds.s3.amazonaws.com", "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"]
    WANT = {("TMP", "2 m above ground"): "t2", ("UGRD", "10 m above ground"): "u10",
            ("VGRD", "10 m above ground"): "v10", ("GUST", "surface"): "gust",
            ("APCP", "surface"): "precip", ("CSNOW", "surface"): "snowfrac",
            ("TCDC", "entire atmosphere"): "cloud",
            ("UGRD", "100 m above ground"): "u100", ("VGRD", "100 m above ground"): "v100",
            ("PRMSL", "mean sea level"): "msl", ("CAPE", "surface"): "cape", ("SNOD", "surface"): "snowdepth",
            ("HGT", "500 mb"): "z500", ("TMP", "850 mb"): "t850"}
    MULTI = {"precip"}          # her adımda birden fazla APCP olabilir

    def url(self, root, run, step, idx=False):
        d, h = run.strftime("%Y%m%d"), run.strftime("%H")
        return f"{root}/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{step:03d}" + (".idx" if idx else "")

    def available(self, client, run):
        return any(_exists(client, self.url(root, run, self.steps[-1], True)) for root in self.ROOTS)

    def fetch_step(self, client, run, step):
        for root in self.ROOTS:
            r = _get(client, self.url(root, run, step, True))
            if r.status_code == 200:
                break
        else:
            return {}
        out: dict[str, list] = {}
        for key, (off, end) in parse_wgrib_idx(r.text, self.WANT, self.MULTI):
            out.setdefault(key, []).append(_range(client, self.url(root, run, step), off, end))
        return out


class IconEu(Model):
    key, label = "icon_eu", "DWD ICON-EU"
    runs, steps, min_lag_h = [0, 12], STEPS_3H_5D, 2
    source = "Deutscher Wetterdienst ICON-EU açık veri (kaynak: DWD)"
    note = "DWD ICON-EU (~7 km) 0,25° ızgarada gösterilir · 3 saatlik adımlar · 5 gün"
    ROOT = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
    SINGLE = {"t_2m": "t2", "u_10m": "u10", "v_10m": "v10", "vmax_10m": "gust",
              "tot_prec": "precip", "snow_gsp": "snow", "snow_con": "snow", "clct": "cloud",
              "pmsl": "msl", "cape_ml": "cape", "h_snow": "snowdepth"}
    PRESSURE = {("fi", 500): "z500", ("t", 850): "t850"}
    sum_snow = True

    def url(self, run, step, var, level=None):
        h = run.strftime("%H")
        kind = "single-level" if level is None else "pressure-level"
        lev = "" if level is None else f"_{level}"
        return (f"{self.ROOT}/{h}/{var}/icon-eu_europe_regular-lat-lon_{kind}_"
                f"{run_id(run)}_{step:03d}{lev}_{var.upper()}.grib2.bz2")

    def available(self, client, run):
        return _exists(client, self.url(run, self.steps[-1], "t_2m"))

    def fetch_step(self, client, run, step):
        out: dict[str, list] = {}
        files = [(v, None, k) for v, k in self.SINGLE.items()] + [(v, lev, k) for (v, lev), k in self.PRESSURE.items()]
        for var, level, key in files:
            r = _get(client, self.url(run, step, var, level))
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"icon_eu {run_id(run)} +{step}h {var}: HTTP {r.status_code}")
            out.setdefault(key, []).append(bz2.decompress(r.content))
        return out


MODELS: dict[str, Model] = {m.key: m for m in [
    EcmwfOpenData("ecmwf", "ECMWF IFS", "ifs", [0, 12], STEPS_3H_7D, 6,
                  "ECMWF IFS HRES açık veri (CC BY 4.0)",
                  "ECMWF IFS 0,25° (~25 km) · 3 saatlik adımlar (6. günden sonra 6 saatlik)"),
    EcmwfOpenData("aifs", "ECMWF AIFS", "aifs-single", [0, 12], STEPS_6H_7D, 5,
                  "ECMWF AIFS Single açık veri (CC BY 4.0)",
                  "ECMWF AIFS (yapay zekâ modeli) 0,25° · 6 saatlik adımlar · hamle verisi yok"),
    Gfs(),
    IconEu(),
]}


# ================================================================ işleme
def model_dir(key: str) -> str:
    return os.path.join(LAYERS_DIR, key)


def latest_meta(key: str = "ecmwf") -> dict | None:
    try:
        with open(os.path.join(model_dir(key), "latest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def process_run(model: Model, client: httpx.Client, run: datetime) -> dict:
    """Koşuyu indirip işler, diske yazar ve meta'yı döndürür."""
    rid, steps = run_id(run), list(model.steps)
    n = len(steps)
    st = status.setdefault(model.key, {})
    out = {k: np.full((n, GRIDS[v["grid"]]["ny"], GRIDS[v["grid"]]["nx"]), np.nan, dtype=np.float32)
           for k, v in VARS.items()}
    acc_p, acc_s = Accum(), Accum()
    got = 0

    for si, step in enumerate(steps):
        st["progress"] = f"{si + 1}/{n}"
        save_status(model.key)
        raw = model.fetch_step(client, run, step)
        f = {}
        for k, msgs in raw.items():
            for m in msgs:
                try:
                    f.setdefault(k, []).append(decode_msg(m, KEY_GRID[k]))
                except Exception as e:                      # tek alan bozuksa koşunun geri kalanı işlensin
                    log.warning("%s %s +%sh %s çözülemedi: %s", model.key, rid, step, k, e)
        # ICON: ızgara ölçekli + konvektif kar aynı aralıkta → tek birikim
        if getattr(model, "sum_snow", False) and len(f.get("snow", [])) == 2:
            (a, ia), (b, ib) = f["snow"]
            if (ia["start"], ia["end"]) == (ib["start"], ib["end"]):
                f["snow"] = [(a + b, ia)]
        one = lambda k: f[k][0] if f.get(k) else None

        if (x := one("t2")) is not None:
            out["temp"][si] = _kelvin_c(*x)
        for k_in, k_out in (("u10", "u"), ("v10", "v"), ("u100", "u100"), ("v100", "v100"), ("cape", "cape")):
            if (x := one(k_in)) is not None:
                out[k_out][si] = x[0]
        if (x := one("gust")) is not None and step > 0:
            out["gust"][si] = x[0]
        if (x := one("cloud")) is not None:
            c = x[0]
            out["cloud"][si] = c * 100.0 if np.nanmax(c) <= 1.5 else c
        if (x := one("snowdepth")) is not None:
            out["snowdepth"][si] = np.clip(_snow_cm(*x), 0, None)
        if (x := one("msl")) is not None:
            out["msl"][si] = x[0] / 100.0 if np.nanmean(x[0]) > 5000 else x[0]
        if (x := one("z500")) is not None:
            out["z500"][si] = _height_m(*x)
        if (x := one("t850")) is not None:
            out["t850"][si] = _kelvin_c(*x)

        p_rate = acc_p.rate([(i["start"], i["end"], _to_mm(a, i)) for a, i in f.get("precip", [])
                             if i["start"] is not None], step)
        s_rate = acc_s.rate([(i["start"], i["end"], _to_mm(a, i)) for a, i in f.get("snow", [])
                             if i["start"] is not None], step)
        if step == 0:
            out["precip"][si] = 0.0
            out["snow"][si] = 0.0
        else:
            if p_rate is not None:
                out["precip"][si] = p_rate
            if s_rate is not None:
                out["snow"][si] = s_rate                       # mm su eşdeğeri ≈ cm kar (10:1)
            elif p_rate is not None and (x := one("snowfrac")) is not None:
                out["snow"][si] = p_rate * np.clip(x[0], 0, 1)  # GFS: yağış × kar olasılığı
        if f:
            got += 1

    if got < n * 0.85:
        raise RuntimeError(f"{model.key} {rid}: {n} adımdan sadece {got} tanesi işlenebildi")

    # İlk adımda hamle yok: bir sonrakini kullan
    if n > 1 and not np.isfinite(out["gust"][0]).any():
        out["gust"][0] = out["gust"][1]
    # Baştaki tamamen boş kareleri at (ör. 0. adımı yayımlanmayan model)
    first = next((i for i in range(n) if np.isfinite(out["temp"][i]).any()), 0)
    if first:
        steps = steps[first:]
        out = {k: v[first:] for k, v in out.items()}

    available = [k for k in VARS if np.isfinite(out[k]).any()]
    mdir = model_dir(model.key)
    os.makedirs(mdir, exist_ok=True)
    tmp, final = os.path.join(mdir, f".tmp-{rid}"), os.path.join(mdir, rid)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for k, spec in VARS.items():
        _to_int16(out[k], spec["scale"]).tofile(os.path.join(tmp, f"{k}.bin"))

    tr = GRIDS["tr"]
    meta = {
        "model": model.key, "label": model.label, "source": model.source, "note": model.note,
        "run_id": rid, "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **{k: tr[k] for k in ("lat_n", "lat_s", "lon_w", "lon_e", "res", "nx", "ny")},
        "grids": GRIDS, "steps": steps,
        "times": [(run + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%MZ") for s in steps],
        "vars": VARS, "available": available, "missing": MISSING,
    }
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    tmp_latest = os.path.join(mdir, "latest.json.tmp")
    with open(tmp_latest, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp_latest, os.path.join(mdir, "latest.json"))

    runs = sorted(d for d in os.listdir(mdir) if d.isdigit() and len(d) == 10)
    for d in runs[:-KEEP_RUNS]:
        shutil.rmtree(os.path.join(mdir, d), ignore_errors=True)

    # Model karnesi: istasyon tahminlerini kaydet (yalnızca 00 UTC koşuları)
    try:
        import verification
        verification.record_forecasts(model.key, run, steps, out["temp"], out["u"], out["v"], GRIDS["tr"])
    except Exception:
        log.exception("karne: istasyon tahminleri kaydedilemedi (%s %s)", model.key, rid)
    return meta


def load_run_arrays(key: str, rid: str, names: list[str]) -> tuple[dict, dict] | None:
    """Diskteki bir koşunun değişkenlerini (gösterim biriminde, NaN'lı) okur."""
    d = os.path.join(model_dir(key), rid)
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    grids = meta.get("grids") or {"tr": {k: meta[k] for k in ("lat_n", "lat_s", "lon_w", "lon_e", "res", "nx", "ny")}}
    arrs = {}
    for nm in names:
        spec = meta["vars"].get(nm)
        p = os.path.join(d, f"{nm}.bin")
        if not spec or not os.path.isfile(p):
            return None
        G = grids[spec.get("grid", "tr")]
        a = np.fromfile(p, dtype="<i2").reshape(len(meta["steps"]), G["ny"], G["nx"]).astype(np.float32)
        a[a == meta.get("missing", MISSING)] = np.nan
        arrs[nm] = a * spec["scale"]
    return meta, arrs


def _migrate_legacy():
    """İlk sürüm (tek model) layers/<koşu> düzenindeydi: ECMWF klasörüne taşı."""
    legacy = os.path.join(LAYERS_DIR, "latest.json")
    if not os.path.isfile(legacy):
        return
    try:
        with open(legacy, encoding="utf-8") as fh:
            meta = json.load(fh)
        src = os.path.join(LAYERS_DIR, meta["run_id"])
        mdir = model_dir("ecmwf")
        if os.path.isdir(src) and not os.path.isfile(os.path.join(mdir, "latest.json")):
            m = MODELS["ecmwf"]
            meta.update({"model": "ecmwf", "label": m.label, "source": m.source, "note": m.note,
                         "available": [k for k in ("temp", "u", "v", "gust", "precip", "snow", "cloud")]})
            os.makedirs(mdir, exist_ok=True)
            os.replace(src, os.path.join(mdir, meta["run_id"]))
            with open(os.path.join(mdir, meta["run_id"], "meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            with open(os.path.join(mdir, "latest.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
    except (OSError, ValueError, KeyError):
        pass
    for d in os.listdir(LAYERS_DIR):
        if d.isdigit() and len(d) == 10:
            shutil.rmtree(os.path.join(LAYERS_DIR, d), ignore_errors=True)
    try:
        os.remove(legacy)
    except OSError:
        pass


def refresh(key: str = "ecmwf", now: datetime | None = None) -> str:
    """Modelin yeni bir koşusu yayımlandıysa indirip işler (bloklayan; arka plan görevinden çağrılır)."""
    model = MODELS[key]
    now = now or datetime.now(timezone.utc)
    if key not in status:                  # yeni süreç: önceki durumu (son hata vb.) dosyadan devral
        status[key] = {"last_check": None, "last_result": None, "last_error": None,
                       "processing": None, "progress": None, **read_status(key)}
    st = status[key]
    os.makedirs(model_dir(key), exist_ok=True)
    st["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lock = open(os.path.join(model_dir(key), ".lock"), "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "başka bir işlem sürüyor"
        with httpx.Client(timeout=90, follow_redirects=True, headers=UA) as client:
            cur = latest_meta(key)
            run = None
            for cand in model.candidate_runs(now):
                if cur and cur.get("run_id", "") >= run_id(cand):
                    break                                   # elimizdeki koşu zaten bu kadar yeni
                if model.available(client, cand):
                    run = cand
                    break
            if run is None:
                st["last_result"] = f"güncel ({cur['run_id']})" if cur else "yayımlanmış koşu bulunamadı"
                return st["last_result"]
            st["processing"] = run_id(run)
            save_status(key)
            t0 = time.time()
            meta = process_run(model, client, run)
            st["last_result"] = f"{meta['run_id']} işlendi ({time.time() - t0:.0f} sn)"
            st["last_error"] = None
            log.info("model haritası %s: %s", key, st["last_result"])
            return st["last_result"]
    except Exception as e:   # arka plan görevi asla çökmesin; eski veri kalır
        st["last_error"] = f"{type(e).__name__}: {e}"
        log.exception("model haritası güncellenemedi: %s", key)
        return st["last_error"]
    finally:
        st["processing"] = None
        st["progress"] = None
        save_status(key)
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def prepare(now: datetime | None = None) -> None:
    """Model işlemeden önceki hafif işler: eski düzeni taşı, karne gözlemleri ve geri doldurma."""
    os.makedirs(LAYERS_DIR, exist_ok=True)
    _migrate_legacy()
    try:                                   # karne: METAR ölçümleri (saatte bir) ve diskteki koşulardan geri doldurma
        import verification
        verification.update_observations(now)
        verification.backfill_from_disk()
    except Exception:
        log.exception("karne güncellenemedi")


def refresh_all(now: datetime | None = None) -> dict:
    prepare(now)
    return {k: refresh(k, now) for k in MODELS}


def models_summary() -> list[dict]:
    out = []
    for k, m in MODELS.items():
        meta = latest_meta(k)
        out.append({"key": k, "label": m.label, "ready": bool(meta),
                    "run": meta and meta["run"], "available": meta and meta.get("available"),
                    "hours": meta and (meta["steps"][-1] if meta["steps"] else 0)})
    return out
