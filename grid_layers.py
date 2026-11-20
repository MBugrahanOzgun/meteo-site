"""Model haritası: ücretsiz/açık sayısal hava tahmini modelleri.

Her model koşusu yayımlandığında sunucu gereken yüzey alanlarını indirir,
Türkiye ve çevresi kutusunu 0,25°'lik ortak ızgaraya keser ve tarayıcının
çizeceği küçük ikili dosyalara (int16) dönüştürür.

  Model           Kaynak (ücretsiz, anahtar yok)                 Lisans
  ECMWF IFS       data.ecmwf.int  (.index + HTTP Range)           CC BY 4.0
  ECMWF AIFS      data.ecmwf.int  (.index + HTTP Range)           CC BY 4.0
  NOAA GFS        noaa-gfs-bdp-pds (AWS)  (.idx + HTTP Range)      Kamu malı
  DWD ICON-EU     opendata.dwd.de  (değişken başına .bz2 dosya)    DWD açık veri (kaynak gösterilerek)

Ortak çıktı değişkenleri (VARS): sıcaklık, rüzgâr u/v, hamle, yağış ve kar
şiddeti (adım aralığının saatlik ortalaması), bulut örtüsü. Birikimli
alanlar (yağış, kar) her GRIB mesajının kendi başlangıç/bitiş adımına göre
şiddete çevrilir; böylece "koşu başından beri" (ECMWF, ICON) ve "6 saatlik
kova" (GFS) birikimleri aynı kodla doğru işlenir.

Diskte (DB_PATH ile aynı kalıcı disk):
  layers/<model>/<koşu>/meta.json, <değişken>.bin   int16 [adım][satır K→G][sütun B→D]
  layers/<model>/latest.json                         en son tamamlanan koşunun meta'sı
"""

import bz2
import fcntl
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

log = logging.getLogger("grid_layers")

# Türkiye ve çevresi (0,25° ızgara noktaları dahil) — tüm modeller için ortak
LAT_N, LAT_S, LON_W, LON_E, RES = 44.0, 34.0, 24.0, 46.0, 0.25
NY = int(round((LAT_N - LAT_S) / RES)) + 1   # 41
NX = int(round((LON_E - LON_W) / RES)) + 1   # 89

# Çıktı değişkenleri: int16 ölçeği ve birimi (değer = tam sayı × ölçek)
VARS = {
    "temp":   {"scale": 0.1,  "unit": "°C"},       # 2 m sıcaklık
    "u":      {"scale": 0.1,  "unit": "m/s"},      # 10 m rüzgâr, doğu bileşeni
    "v":      {"scale": 0.1,  "unit": "m/s"},      # 10 m rüzgâr, kuzey bileşeni
    "gust":   {"scale": 0.1,  "unit": "m/s"},      # hamle
    "precip": {"scale": 0.01, "unit": "mm/h"},     # adım aralığında ortalama yağış şiddeti
    "snow":   {"scale": 0.01, "unit": "cm/h"},     # kar yağışı (su eşdeğeri mm ≈ cm kar, 10:1)
    "cloud":  {"scale": 1,    "unit": "%"},        # toplam bulut örtüsü
}
MISSING = -32768

DATA_DIR = os.path.dirname(os.path.abspath(os.environ.get("DB_PATH", "meteo.db")))
LAYERS_DIR = os.path.join(DATA_DIR, "layers")
KEEP_RUNS = 2
UA = {"User-Agent": "forecaster.com.tr/1.0 (model map)"}

STEPS_3H_7D = list(range(0, 145, 3)) + list(range(150, 169, 6))   # 53 adım, 168 sa
STEPS_6H_7D = list(range(0, 169, 6))                              # 29 adım
STEPS_3H_5D = list(range(0, 121, 3))                              # 41 adım

# /layers/status için son durum (hata ayıklama), model başına
status: dict[str, dict] = {}


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


def decode_msg(msg: bytes) -> tuple[np.ndarray, dict]:
    """Tek GRIB mesajını çözer; Türkiye kutusunu (NY×NX, kuzeyden güneye) ve
    birim / adım bilgisini döndürür."""
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
    return vals[np.ix_(rows, cols)], info


def decode_crop(msg: bytes) -> np.ndarray:
    return decode_msg(msg)[0]


def _to_mm(arr: np.ndarray, info: dict) -> np.ndarray:
    """Yağış/kar birikimini mm'ye (kg/m²) çevirir: ECMWF IFS metre, diğerleri kg/m²."""
    u = (info.get("units") or "").strip().lower()
    return arr * 1000.0 if u in ("m", "m of water equivalent") else arr


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
        while len(out) < 6:
            if t.hour in self.runs and now - t >= timedelta(hours=self.min_lag_h):
                out.append(t)
            t -= timedelta(hours=1)
        return out

    def available(self, client, run) -> bool:
        raise NotImplementedError

    def fetch_step(self, client, run, step) -> dict[str, list[tuple[np.ndarray, dict]]]:
        """Bir adımın alanları: {"t2"|"u10"|"v10"|"gust"|"cloud"|"precip"|"snow"|"snowfrac": [(dizi, bilgi), …]}"""
        raise NotImplementedError


class EcmwfOpenData(Model):
    ROOT = "https://data.ecmwf.int/forecasts"
    PARAMS = {"2t": "t2", "10u": "u10", "10v": "v10", "10fg": "gust", "tp": "precip", "sf": "snow", "tcc": "cloud"}

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
        for e in parse_ecmwf_index(r.text, self.PARAMS):
            body = _range(client, self.url(run, step, "grib2"), e["_offset"], e["_offset"] + e["_length"] - 1)
            out.setdefault(self.PARAMS[e["param"]], []).append(decode_msg(body))
        return out


def parse_ecmwf_index(text: str, params: dict) -> list[dict]:
    """ECMWF .index (her satır bir JSON) → istenen yüzey alanlarının girdileri."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("levtype") == "sfc" and e.get("param") in params:
            e["_offset"], e["_length"] = int(e["_offset"]), int(e["_length"])
            out.append(e)
    return out


class Gfs(Model):
    key, label = "gfs", "NOAA GFS"
    runs, steps, min_lag_h = [0, 6, 12, 18], STEPS_3H_7D, 3
    source = "NOAA/NCEP GFS 0,25° (kamu malı)"
    note = "NOAA GFS 0,25° (~25 km) · 3 saatlik adımlar (6. günden sonra 6 saatlik)"
    ROOTS = ["https://noaa-gfs-bdp-pds.s3.amazonaws.com", "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"]
    WANT = {("TMP", "2 m above ground"): "t2", ("UGRD", "10 m above ground"): "u10",
            ("VGRD", "10 m above ground"): "v10", ("GUST", "surface"): "gust",
            ("APCP", "surface"): "precip", ("CSNOW", "surface"): "snowfrac",
            ("TCDC", "entire atmosphere"): "cloud"}
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
        for var, (off, end) in parse_wgrib_idx(r.text, self.WANT, self.MULTI):
            body = _range(client, self.url(root, run, step), off, end)
            out.setdefault(var, []).append(decode_msg(body))
        return out


def parse_wgrib_idx(text: str, want: dict, multi: set) -> list[tuple[str, tuple[int, int | None]]]:
    """NCEP .idx ("n:bayt:d=..:DEĞİŞKEN:SEVİYE:zaman:") → [(çıktı adı, (başlangıç, bitiş))]."""
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


class IconEu(Model):
    key, label = "icon_eu", "DWD ICON-EU"
    runs, steps, min_lag_h = [0, 6, 12, 18], STEPS_3H_5D, 2
    source = "Deutscher Wetterdienst ICON-EU açık veri (kaynak: DWD)"
    note = "DWD ICON-EU (~7 km) 0,25° ızgarada gösterilir · 3 saatlik adımlar · 5 gün"
    ROOT = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
    VARS = {"t_2m": "t2", "u_10m": "u10", "v_10m": "v10", "vmax_10m": "gust",
            "tot_prec": "precip", "snow_gsp": "snow", "snow_con": "snow", "clct": "cloud"}

    def url(self, run, step, var):
        h = run.strftime("%H")
        return (f"{self.ROOT}/{h}/{var}/icon-eu_europe_regular-lat-lon_single-level_"
                f"{run_id(run)}_{step:03d}_{var.upper()}.grib2.bz2")

    def available(self, client, run):
        return _exists(client, self.url(run, self.steps[-1], "t_2m"))

    def fetch_step(self, client, run, step):
        out: dict[str, list] = {}
        for var, key in self.VARS.items():
            r = _get(client, self.url(run, step, var))
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"icon_eu {run_id(run)} +{step}h {var}: HTTP {r.status_code}")
            out.setdefault(key, []).append(decode_msg(bz2.decompress(r.content)))
        # kar = ızgara ölçekli + konvektif kar (aynı aralık): tek birikime topla
        if len(out.get("snow", [])) == 2:
            (a, ia), (b, ib) = out["snow"]
            if (ia["start"], ia["end"]) == (ib["start"], ib["end"]):
                out["snow"] = [(a + b, ia)]
        return out


MODELS: dict[str, Model] = {m.key: m for m in [
    EcmwfOpenData("ecmwf", "ECMWF IFS", "ifs", [0, 12], STEPS_3H_7D, 6,
                  "ECMWF IFS HRES açık veri (CC BY 4.0)",
                  "ECMWF IFS 0,25° (~25 km) · 3 saatlik adımlar (6. günden sonra 6 saatlik)"),
    EcmwfOpenData("aifs", "ECMWF AIFS", "aifs-single", [0, 6, 12, 18], STEPS_6H_7D, 5,
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
    out = {k: np.full((n, NY, NX), np.nan, dtype=np.float32) for k in VARS}
    acc_p, acc_s = Accum(), Accum()
    got = 0

    for si, step in enumerate(steps):
        st["progress"] = f"{si + 1}/{n}"
        f = model.fetch_step(client, run, step)
        one = lambda k: f[k][0] if f.get(k) else None

        if (x := one("t2")) is not None:
            a, info = x
            out["temp"][si] = a - 273.15 if info["units"] == "K" or np.nanmean(a) > 150 else a
        if (x := one("u10")) is not None:
            out["u"][si] = x[0]
        if (x := one("v10")) is not None:
            out["v"][si] = x[0]
        if (x := one("gust")) is not None and step > 0:
            out["gust"][si] = x[0]
        if (x := one("cloud")) is not None:
            c = x[0]
            out["cloud"][si] = c * 100.0 if np.nanmax(c) <= 1.5 else c

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

    meta = {
        "model": model.key, "label": model.label, "source": model.source, "note": model.note,
        "run_id": rid, "run": run.strftime("%Y-%m-%dT%H:%MZ"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lat_n": LAT_N, "lat_s": LAT_S, "lon_w": LON_W, "lon_e": LON_E, "res": RES,
        "nx": NX, "ny": NY, "steps": steps,
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
    return meta


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
                         "available": list(VARS)})
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
    st = status.setdefault(key, {"last_check": None, "last_result": None, "last_error": None,
                                 "processing": None, "progress": None})
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
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def refresh_all(now: datetime | None = None) -> dict:
    os.makedirs(LAYERS_DIR, exist_ok=True)
    _migrate_legacy()
    return {k: refresh(k, now) for k in MODELS}


def models_summary() -> list[dict]:
    out = []
    for k, m in MODELS.items():
        meta = latest_meta(k)
        out.append({"key": k, "label": m.label, "ready": bool(meta),
                    "run": meta and meta["run"], "available": meta and meta.get("available"),
                    "hours": meta and (meta["steps"][-1] if meta["steps"] else 0)})
    return out
