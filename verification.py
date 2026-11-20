"""Model doğruluk karnesi.

Modellerin 00 UTC koşularının 1–3 gün sonrası için yaptığı 2 m sıcaklık ve
10 m rüzgâr tahminleri, Türkiye'deki havalimanlarının resmi METAR
ölçümleriyle karşılaştırılır.

  Gözlem : aviationweather.gov Data API (METAR, ücretsiz, anahtar yok)
  Tahmin : grid_layers'ın işlediği model ızgaralarından istasyon konumuna
           çift doğrusal enterpolasyon (kayıt: her 00 UTC koşusu işlendiğinde)

Adil karşılaştırma için bir (istasyon, saat, tahmin günü) örneği ancak
karşılaştırılan modellerin hepsinin tahmini varsa sayılır. Böylece her model
aynı vakalarla ölçülür (AIFS 6 saatlik olduğundan ortak saatler 6 saatte bir).

Veri DB_PATH ile aynı kalıcı diskte, ayrı bir SQLite dosyasında tutulur.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

log = logging.getLogger("verification")

AVWX_METAR = "https://aviationweather.gov/api/data/metar"
DATA_DIR = os.path.dirname(os.path.abspath(os.environ.get("DB_PATH", "meteo.db")))
DB_FILE = os.path.join(DATA_DIR, "verification.db")
LEAD_MAX = 72            # 3 gün
KEEP_DAYS = 40
REGIONS = ["Marmara", "Ege", "Akdeniz", "İç Anadolu", "Karadeniz", "Doğu Anadolu", "Güneydoğu Anadolu"]
ALL = "Türkiye"

# METAR yayımlayan Türk havalimanları → coğrafi bölge (konumlar METAR'dan okunur)
STATIONS = {
    # Marmara
    "LTFM": "Marmara", "LTFJ": "Marmara", "LTBA": "Marmara", "LTBU": "Marmara", "LTBR": "Marmara",
    "LTBH": "Marmara", "LTFD": "Marmara", "LTBQ": "Marmara", "LTBF": "Marmara",
    # Ege
    "LTBJ": "Ege", "LTBS": "Ege", "LTFE": "Ege", "LTAY": "Ege", "LTBO": "Ege", "LTBZ": "Ege", "LTAH": "Ege",
    # Akdeniz
    "LTAI": "Akdeniz", "LTFG": "Akdeniz", "LTAF": "Akdeniz", "LTDB": "Akdeniz", "LTDA": "Akdeniz",
    "LTCN": "Akdeniz", "LTFC": "Akdeniz", "LTAG": "Akdeniz",
    # İç Anadolu
    "LTAC": "İç Anadolu", "LTAN": "İç Anadolu", "LTAU": "İç Anadolu", "LTAR": "İç Anadolu",
    "LTAZ": "İç Anadolu", "LTBY": "İç Anadolu", "LTAE": "İç Anadolu", "LTAB": "İç Anadolu",
    # Karadeniz
    "LTCG": "Karadeniz", "LTFH": "Karadeniz", "LTAP": "Karadeniz", "LTAS": "Karadeniz", "LTCB": "Karadeniz",
    "LTAL": "Karadeniz", "LTCM": "Karadeniz", "LTAW": "Karadeniz", "LTFO": "Karadeniz",
    # Doğu Anadolu
    "LTCE": "Doğu Anadolu", "LTCI": "Doğu Anadolu", "LTCA": "Doğu Anadolu", "LTAT": "Doğu Anadolu",
    "LTCK": "Doğu Anadolu", "LTCF": "Doğu Anadolu", "LTCD": "Doğu Anadolu", "LTCO": "Doğu Anadolu",
    "LTCT": "Doğu Anadolu", "LTCU": "Doğu Anadolu", "LTCW": "Doğu Anadolu",
    # Güneydoğu Anadolu
    "LTCC": "Güneydoğu Anadolu", "LTAJ": "Güneydoğu Anadolu", "LTCS": "Güneydoğu Anadolu",
    "LTCR": "Güneydoğu Anadolu", "LTCJ": "Güneydoğu Anadolu", "LTCL": "Güneydoğu Anadolu",
    "LTCP": "Güneydoğu Anadolu", "LTCV": "Güneydoğu Anadolu",
}

_state = {"last_obs": 0.0, "backfilled": set(), "cache": None, "cache_t": 0.0}
_lock = threading.Lock()


# ================================================================ veritabanı
def _db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS stations(icao TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, elev REAL, region TEXT);
        CREATE TABLE IF NOT EXISTS obs(icao TEXT, hour INTEGER, dt INTEGER, temp REAL, wind REAL, PRIMARY KEY(icao, hour));
        CREATE TABLE IF NOT EXISTS fc(model TEXT, run INTEGER, icao TEXT, valid INTEGER, lead INTEGER,
                                      temp REAL, wind REAL, PRIMARY KEY(model, run, icao, valid));
        CREATE INDEX IF NOT EXISTS fc_valid ON fc(valid);
    """)
    return con


# ================================================================ gözlemler (METAR)
def parse_metars(items: list) -> tuple[list, list]:
    """AWC METAR JSON listesi → (istasyon satırları, gözlem satırları)."""
    stations, obs = {}, []
    for m in items or []:
        icao = (m.get("icaoId") or "").upper()
        if icao not in STATIONS:
            continue
        if m.get("lat") is not None and m.get("lon") is not None:
            stations[icao] = (icao, m.get("name") or icao, float(m["lat"]), float(m["lon"]),
                              m.get("elev"), STATIONS[icao])
        t = m.get("obsTime")
        if t is None:
            continue
        t = int(t)
        hour = int(round(t / 3600.0)) * 3600
        dt = abs(t - hour)
        if dt > 20 * 60:                         # saat başına 20 dakikadan uzak raporları kullanma
            continue
        temp = m.get("temp")
        wspd = m.get("wspd")
        obs.append((icao, hour, dt, None if temp is None else float(temp),
                    None if wspd is None else float(wspd) * 1.852))   # knot → km/sa
    return list(stations.values()), obs


def update_observations(now: datetime | None = None, force: bool = False) -> int:
    """Son METAR'ları çekip kaydeder (en fazla saatte bir). Kaydedilen gözlem sayısını döndürür."""
    ts = (now or datetime.now(timezone.utc)).timestamp()
    with _lock:
        if not force and ts - _state["last_obs"] < 50 * 60:
            return 0
        _state["last_obs"] = ts
    con = _db()
    try:
        empty = con.execute("SELECT COUNT(*) FROM obs").fetchone()[0] == 0
        hours = 48 if empty else 8
        ids = list(STATIONS)
        n = 0
        with httpx.Client(timeout=40, headers={"User-Agent": "forecaster.com.tr/1.0 (model verification)"}) as client:
            for i in range(0, len(ids), 20):
                r = client.get(AVWX_METAR, params={"ids": ",".join(ids[i:i + 20]), "format": "json", "hours": hours})
                if r.status_code == 204 or not r.content:
                    continue
                r.raise_for_status()
                data = r.json()
                st_rows, ob_rows = parse_metars(data if isinstance(data, list) else [])
                con.executemany("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?)", st_rows)
                con.executemany("""INSERT INTO obs VALUES (?,?,?,?,?)
                                   ON CONFLICT(icao, hour) DO UPDATE SET dt=excluded.dt, temp=excluded.temp, wind=excluded.wind
                                   WHERE excluded.dt < obs.dt""", ob_rows)
                n += len(ob_rows)
        cutoff = int(ts) - KEEP_DAYS * 86400
        con.execute("DELETE FROM obs WHERE hour < ?", (cutoff,))
        con.execute("DELETE FROM fc WHERE valid < ?", (cutoff,))
        con.commit()
        return n
    finally:
        con.close()


# ================================================================ tahminler
def _bilinear(arr: np.ndarray, grid: dict, lat: float, lon: float) -> np.ndarray | None:
    """arr [adım][ny][nx] → istasyondaki zaman serisi (adım sayısı uzunluğunda)."""
    fj = (grid["lat_n"] - lat) / grid["res"]
    fi = (lon - grid["lon_w"]) / grid["res"]
    if not (0 <= fj <= grid["ny"] - 1 and 0 <= fi <= grid["nx"] - 1):
        return None
    j0, i0 = int(fj), int(fi)
    j1, i1 = min(j0 + 1, grid["ny"] - 1), min(i0 + 1, grid["nx"] - 1)
    dj, di = fj - j0, fi - i0
    return ((arr[:, j0, i0] * (1 - di) + arr[:, j0, i1] * di) * (1 - dj)
            + (arr[:, j1, i0] * (1 - di) + arr[:, j1, i1] * di) * dj)


def record_forecasts(model: str, run: datetime, steps: list, temp, u, v, grid: dict) -> int:
    """00 UTC koşusunun istasyon tahminlerini (1–72 sa) kaydeder."""
    if run.hour != 0:
        return 0
    con = _db()
    try:
        stations = con.execute("SELECT icao, lat, lon FROM stations").fetchall()
        if not stations:
            return 0
        run_ts = int(run.replace(tzinfo=timezone.utc).timestamp()) if run.tzinfo is None else int(run.timestamp())
        idx = [k for k, s in enumerate(steps) if 0 < s <= LEAD_MAX]
        if not idx:
            return 0
        T = np.asarray(temp)[idx]
        W = np.hypot(np.asarray(u)[idx], np.asarray(v)[idx]) * 3.6       # m/s → km/sa
        rows = []
        for icao, lat, lon in stations:
            ts, ws = _bilinear(T, grid, lat, lon), _bilinear(W, grid, lat, lon)
            if ts is None:
                continue
            for n, k in enumerate(idx):
                t, w = float(ts[n]), float(ws[n])
                rows.append((model, run_ts, icao, run_ts + steps[k] * 3600, steps[k],
                             None if np.isnan(t) else round(t, 2), None if np.isnan(w) else round(w, 2)))
        con.executemany("INSERT OR REPLACE INTO fc VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        return len(rows)
    finally:
        con.close()


def backfill_from_disk() -> int:
    """Diskteki 00 UTC koşularından henüz kaydedilmemiş istasyon tahminlerini kaydeder."""
    import grid_layers as GL
    total = 0
    con = _db()
    try:
        if not con.execute("SELECT 1 FROM stations LIMIT 1").fetchone():
            return 0
        for key in GL.MODELS:
            mdir = GL.model_dir(key)
            if not os.path.isdir(mdir):
                continue
            for rid in sorted(d for d in os.listdir(mdir) if d.isdigit() and len(d) == 10 and d.endswith("00")):
                if (key, rid) in _state["backfilled"]:
                    continue
                run = datetime.strptime(rid, "%Y%m%d%H").replace(tzinfo=timezone.utc)
                have = con.execute("SELECT 1 FROM fc WHERE model=? AND run=? LIMIT 1",
                                   (key, int(run.timestamp()))).fetchone()
                if not have:
                    loaded = GL.load_run_arrays(key, rid, ["temp", "u", "v"])
                    if loaded:
                        meta, a = loaded
                        grid = (meta.get("grids") or {}).get("tr") or GL.GRIDS["tr"]
                        total += record_forecasts(key, run, meta["steps"], a["temp"], a["u"], a["v"], grid)
                _state["backfilled"].add((key, rid))
    finally:
        con.close()
    return total


# ================================================================ skorlar
def _tr_date(ts: int) -> str:
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


def scores(days: int = 7, now: datetime | None = None) -> dict:
    """Son `days` gün için model hataları: tahmin günü × değişken × bölge × model."""
    import grid_layers as GL
    nowts = int((now or datetime.now(timezone.utc)).timestamp())
    with _lock:
        c = _state["cache"]
        if c and c[0] == (days, nowts // 600):
            return c[1]
    since = nowts - days * 86400
    con = _db()
    try:
        rows = con.execute("""
            SELECT f.model, f.icao, f.valid, f.lead, f.temp, f.wind, o.temp, o.wind, s.region
            FROM fc f JOIN obs o ON o.icao = f.icao AND o.hour = f.valid
                      JOIN stations s ON s.icao = f.icao
            WHERE f.valid >= ? AND f.valid <= ? AND f.lead BETWEEN 1 AND ?""", (since, nowts, LEAD_MAX)).fetchall()
        n_fc = con.execute("SELECT COUNT(*) FROM fc").fetchone()[0]
        n_obs = con.execute("SELECT COUNT(*) FROM obs WHERE hour >= ?", (since,)).fetchone()[0]
        n_st = con.execute("SELECT COUNT(DISTINCT icao) FROM obs WHERE hour >= ?", (since,)).fetchone()[0]
    finally:
        con.close()

    # vaka: (değişken, gün, istasyon, saat) → {model: hata}
    cases: dict = {}
    region_of = {}
    for model, icao, valid, lead, ft, fw, ot, ow, region in rows:
        ld = (lead - 1) // 24 + 1
        region_of[icao] = region
        if ft is not None and ot is not None:
            cases.setdefault(("temp", ld, icao, valid), {})[model] = ft - ot
        if fw is not None and ow is not None:
            cases.setdefault(("wind", ld, icao, valid), {})[model] = fw - ow

    models = [k for k in GL.MODELS]
    results, daily, excluded = {}, {}, {}
    for var in ("temp", "wind"):
        results[var], excluded[var] = {}, {}
        for ld in (1, 2, 3):
            cs = [(k, m) for k, m in cases.items() if k[0] == var and k[1] == ld]
            cover = {mk: sum(1 for _, m in cs if mk in m) for mk in models}
            top = max(cover.values()) if cover else 0
            use = [mk for mk in models if top and cover[mk] >= 0.5 * top]     # verisi çok az olan modeli dışarıda bırak
            excluded[var][str(ld)] = [mk for mk in models if top and mk not in use and cover[mk] > 0]
            common = [(k, m) for k, m in cs if use and all(mk in m for mk in use)]
            res = {}
            for reg in [ALL] + REGIONS:
                sub = [m for k, m in common if reg == ALL or region_of.get(k[2]) == reg]
                if not sub:
                    continue
                best = [min(abs(m[mk]) for mk in use) for m in sub]
                res[reg] = {}
                for mk in use:
                    e = np.array([m[mk] for m in sub])
                    wins = sum(1 for m, b in zip(sub, best) if abs(abs(m[mk]) - b) < 1e-9)
                    res[reg][mk] = {"mae": round(float(np.mean(np.abs(e))), 2), "bias": round(float(np.mean(e)), 2),
                                    "n": len(sub), "wins": round(wins / len(sub), 3)}
            results[var][str(ld)] = res
            if ld == 1:
                by_day: dict = {}
                for k, m in common:
                    by_day.setdefault(_tr_date(k[3]), []).append(m)
                daily[var] = {d: {mk: round(float(np.mean([abs(m[mk]) for m in ms])), 2) for mk in use}
                              for d, ms in sorted(by_day.items())}

    out = {
        "generated_at": datetime.fromtimestamp(nowts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "days": days,
        "period": {"from": datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                   "to": datetime.fromtimestamp(nowts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")},
        "counts": {"forecasts": n_fc, "observations": n_obs, "stations": n_st, "matched": len(rows)},
        "models": [{"key": k, "label": GL.MODELS[k].label} for k in models],
        "regions": [ALL] + REGIONS,
        "results": results, "daily": daily, "excluded": excluded,
    }
    with _lock:
        _state["cache"] = ((days, nowts // 600), out)
    return out
