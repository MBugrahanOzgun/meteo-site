"""Model haritası işleyicisi: web sunucusundan AYRI bir süreçte çalışır.

main.py her 20 dakikada bir bu betiği önce "--prep" ile (karne ölçümleri),
sonra her model için ayrı ayrı çalıştırır:

    python layer_worker.py --prep
    python layer_worker.py ecmwf

Neden ayrı süreç: GRIB indirme/çözme bellek ve işlemci yoğun bir iş. Web
sunucusunun içinde çalıştığında bir bellek taşması ya da C kütüphanesindeki
bir çökme bütün siteyi düşürüp 502 hatası verdirebiliyordu. Ayrı süreçte
en kötü ihtimalle sadece bu işlem ölür; site çalışmaya devam eder, haritalar
bir önceki koşuyla gösterilir. Süreç bitince belleğin tamamı işletim
sistemine geri döner.
"""

import logging
import os
import sys


def _be_polite():
    """Web sunucusu her zaman öncelikli olsun; bellek sıkışırsa önce bu süreç feda edilsin."""
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s [layer_worker] %(name)s %(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # her indirme isteği loga düşmesin
    _be_polite()
    import grid_layers

    if not argv:
        print("kullanım: layer_worker.py --prep | <model> [<model> ...]", file=sys.stderr)
        return 2
    if argv == ["--prep"]:
        grid_layers.prepare()
        return 0
    for key in argv:
        if key not in grid_layers.MODELS:
            print(f"bilinmeyen model: {key}", file=sys.stderr)
            return 2
        print(f"[layer_worker] {key}: {grid_layers.refresh(key)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
