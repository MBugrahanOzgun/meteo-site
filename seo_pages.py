"""Sayfa adresleri ve arama motoru (SEO) bilgileri.

Sitedeki her sekme ve Danışmanlık sekmesindeki her panel kendi adresinden
açılır (örn. /hava-durumu, /danismanlik/findik). Google her adresi ayrı bir
sayfa olarak görür; başlığı (title) ve açıklaması (description) aşağıdaki
tablodan gelir.

Alanlar:
  path   – sayfanın adresi
  page   – index.html'deki section[data-page] değeri
  tab    – (varsa) Danışmanlık sekmesindeki panelin data-tab değeri
  label  – sayfanın altındaki link listesinde görünen kısa ad
  title  – Google sonuçlarında ve tarayıcı sekmesinde görünen başlık (~60 karakter)
  desc   – Google sonuçlarındaki açıklama (~155 karakter)

Başlık/açıklama değiştirmek, yeni bir sayfa eklemek için sadece bu tabloyu
düzenlemek yeterli; sitemap.xml ve alt kısımdaki link listesi buradan üretilir.
"""

import html
import json
import os
import re

SITE_URL = "https://forecaster.com.tr"
INDEX_PATH = os.path.join("static", "index.html")

SEO_ROUTES = [
    # ---- Ana sekmeler ----
    {"path": "/", "page": "ozet", "label": "Ana Sayfa",
     "title": "Forecaster | Meteoroloji Danışmanlığı ve Sektörel Hava Tahmini",
     "desc": "Yenilenebilir enerji, denizcilik, inşaat ve tarım sektörleri için meteoroloji danışmanlığı: canlı hava durumu, 7 günlük tahmin, hava kalitesi ve deniz verileri."},
    {"path": "/hava-durumu", "page": "hava", "label": "Hava Durumu",
     "title": "Hava Durumu ve 7 Günlük Tahmin | Forecaster",
     "desc": "Seçtiğiniz konum için anlık hava durumu ve 7 günlük tahmin: hissedilen sıcaklık, nem, basınç, rüzgâr, yağış olasılığı, UV ve güneş radyasyonu."},
    {"path": "/uyarilar", "page": "uyarilar", "label": "Uyarılar",
     "title": "Meteorolojik Uyarılar ve Türkiye Uyarı Haritası | Forecaster",
     "desc": "Türkiye uyarı haritası ve il, ilçe bazlı meteorolojik uyarılar; önümüzdeki günler için yağış, rüzgâr, sıcaklık ve orman yangını riskleri."},
    {"path": "/tahmin-haritalari", "page": "haritalar", "label": "Tahmin Haritaları",
     "title": "Yağış ve Rüzgâr Tahmin Haritaları – 81 İl | Forecaster",
     "desc": "81 il için 7 günlük yağış ve rüzgâr hızı tahmin haritaları. Bir gün seçin, iller tahmine göre renklensin; ile tıklayarak 7 günlük seyri görün."},
    {"path": "/tahmin-grafikleri", "page": "grafik", "label": "Tahmin Grafikleri",
     "title": "168 Saatlik Hava Tahmini Grafikleri | Forecaster",
     "desc": "Seçili konum için 7 günlük (168 saat) saatlik sıcaklık, nem, basınç, rüzgâr, bulut örtüsü ve yağış tahmini grafikleri."},
    {"path": "/hava-kalitesi", "page": "kalite", "label": "Hava Kalitesi",
     "title": "Hava Kalitesi: PM2.5, PM10 ve Ozon | Forecaster",
     "desc": "Seçili konum için anlık hava kalitesi: PM2.5, PM10, ozon (O₃), NO₂, CO ve Avrupa Hava Kalitesi İndeksi (EU-AQI) değerleri."},
    {"path": "/deniz", "page": "deniz", "label": "Deniz",
     "title": "Deniz Tahmini: Dalga Yüksekliği ve Swell | Forecaster",
     "desc": "Kıyı ve açık deniz konumları için dalga yüksekliği, dalga yönü, periyot ve swell tahminleri."},
    # /danismanlik: Danışmanlık sekmesini "Tüm Hizmetler" özetiyle açar.
    {"path": "/danismanlik", "page": "danismanlik", "tab": "services", "label": "Danışmanlık",
     "title": "Sektörel Meteoroloji Danışmanlığı | Forecaster",
     "desc": "Ham hava verisini karara çeviren sektörel paneller: tarımda don ve ilaçlama, enerji üretimi, şantiye, denizcilik, havacılık ve daha fazlası."},
    {"path": "/uzman-notu", "page": "uzmannotu", "label": "Uzman Notu",
     "title": "Uzman Notu – Güncel Meteorolojik Değerlendirme | Forecaster",
     "desc": "Meteoroloji uzmanımızın güncel hava değerlendirmeleri ve takip ettiği veriler."},
    {"path": "/iletisim", "page": "iletisim", "label": "İletişim",
     "title": "İletişim – Meteoroloji Danışmanlığı Alın | Forecaster",
     "desc": "Sektörünüze özel meteoroloji danışmanlığı ve raporlar için Forecaster ile iletişime geçin."},
    {"path": "/gizlilik", "page": "gizlilik", "label": "Gizlilik Politikası",
     "title": "Gizlilik Politikası | Forecaster",
     "desc": "Forecaster gizlilik politikası: konum bilgisi, çerezler ve kullanılan üçüncü taraf servisler."},

    # ---- Danışmanlık panelleri ----
    {"path": "/danismanlik/tarim", "page": "danismanlik", "tab": "agriculture", "label": "Tarım",
     "title": "Tarım Meteorolojisi ve Zirai Hava Tahmini | Forecaster",
     "desc": "Açık alan ve sera üretimi için tarım meteorolojisi danışmanlığı: tarımı etkileyen hava koşullarını önceden tahmin ederek önlem ve ticari planlama."},
    {"path": "/danismanlik/findik", "page": "danismanlik", "tab": "hazelnut", "label": "Fındık",
     "title": "Fındık Don Riski ve İlaçlama Takvimi | Forecaster",
     "desc": "Fındığın fenolojik dönemine göre don eşikleri, ilaçlama pencereleri, harman-kurutma uygunluğu ve külleme riski; bahçenizin konumuna özel canlı panel."},
    {"path": "/danismanlik/bugday", "page": "danismanlik", "tab": "crop-bugday", "label": "Buğday",
     "title": "Buğday Don Riski ve Hasat Uygunluğu | Forecaster",
     "desc": "Kardeşlenme, sapa kalkma ve başaklanma dönemlerine göre buğdayda don eşikleri, sarı pas riski ve hasat/yağış uygunluğu."},
    {"path": "/danismanlik/uzum", "page": "danismanlik", "tab": "crop-uzum", "label": "Üzüm",
     "title": "Bağda Don, Mildiyö ve Külleme Riski | Forecaster",
     "desc": "Kış dinlenmesinden hasada kadar dönemlere göre bağda don eşikleri, mildiyö/külleme riski ve kuru üzüm kurutma uygunluğu."},
    {"path": "/danismanlik/kayisi", "page": "danismanlik", "tab": "crop-kayisi", "label": "Kayısı",
     "title": "Kayısıda Geç Don ve Monilya Riski | Forecaster",
     "desc": "İlkbahar geç donlarından en çok etkilenen meyve olan kayısı için fenolojik döneme göre don eşikleri ve monilya (çil) riski."},
    {"path": "/danismanlik/toprak-nemi", "page": "danismanlik", "tab": "soil", "label": "Toprak Nemi ve Kuraklık",
     "title": "Toprak Nemi, Kuraklık ve Sulama Tahmini | Forecaster",
     "desc": "Yüzey ve kök bölgesi toprak nemi, son 30 günün su dengesi ve önümüzdeki 7 gün için kuraklık ve sulama değerlendirmesi."},
    {"path": "/danismanlik/sut-uretimi", "page": "danismanlik", "tab": "dairy", "label": "Süt Üretimi",
     "title": "Süt Sığırlarında Isı Stresi (THI) | Forecaster",
     "desc": "Sıcaklık-Nem İndeksi (THI) ile süt sığırlarında ısı stresi takibi: il ve ilçe bazında önümüzdeki 3 günün saatlik değerleri."},
    {"path": "/danismanlik/enerji", "page": "danismanlik", "tab": "energy", "label": "Yenilenebilir Enerji",
     "title": "Yenilenebilir Enerji Meteorolojisi (RES/GES) | Forecaster",
     "desc": "Rüzgâr ve güneş enerjisi tesisleri için meteoroloji danışmanlığı: saatlikten mevsimliğe tahminler, saha analizi ve tesis istasyonu verisiyle değerlendirme."},
    {"path": "/danismanlik/ges-res", "page": "danismanlik", "tab": "energy-live", "label": "GES ve RES",
     "title": "GES ve RES için Güneş ve Rüzgâr Tahmini | Forecaster",
     "desc": "Güneş ve rüzgâr santralleri için saatlik meteorolojik eşik analizi: beklenen radyasyon, sıcaklığa bağlı panel kayıpları ve türbin çalışma aralıkları."},
    {"path": "/danismanlik/hes", "page": "danismanlik", "tab": "hydro", "label": "HES (Nehir Debisi)",
     "title": "HES için Nehir Debisi Tahmini | Forecaster",
     "desc": "Seçili noktada nehir debisi: son 30 günün verisi, ileri tahmin, uzun dönem medyanıyla karşılaştırma ve basit bir HES üretim hesaplayıcısı."},
    {"path": "/danismanlik/insaat", "page": "danismanlik", "tab": "construction-info", "label": "İnşaat",
     "title": "İnşaat Meteorolojisi ve Şantiye Hava Tahmini | Forecaster",
     "desc": "İnşaat projelerinin her aşamasında belirleyici olan hava koşulları için danışmanlık: hangi parametrenin hangi vadede izleneceği ve şantiye planlaması."},
    {"path": "/danismanlik/santiye", "page": "danismanlik", "tab": "construction", "label": "Beton Döküm ve Şantiye",
     "title": "Beton Döküm, Vinç ve Şantiye Hava Uygunluğu | Forecaster",
     "desc": "Beton dökümü uygunluğu, vinç ve yüksekte çalışma rüzgâr riski ve şantiye çalışma takvimi; TS EN 206, TS 1248 ve iş güvenliği standartlarına göre."},
    {"path": "/danismanlik/denizcilik", "page": "danismanlik", "tab": "marine", "label": "Denizcilik",
     "title": "Denizcilik için Rüzgâr ve Dalga Tahmini | Forecaster",
     "desc": "Kıyı şeridi boyunca uzman tahmincilerle 3 saatlik periyotlarda rüzgâr yönü ve hızı, dalga yüksekliği ve hava tahmini desteği."},
    {"path": "/danismanlik/havacilik", "page": "danismanlik", "tab": "aviation", "label": "Havacılık",
     "title": "Havacılık Meteorolojisi: METAR ve TAF | Forecaster",
     "desc": "Havaalanlarının resmi anlık hava raporu (METAR) ve 24–30 saatlik tahmini (TAF); dünya çapındaki meydanlar için güncel veriler."},
    {"path": "/danismanlik/stadyum", "page": "danismanlik", "tab": "stadium", "label": "Stadyum",
     "title": "Stadyum ve Maç Günü Hava Riski | Forecaster",
     "desc": "Maç ve etkinlikler için yıldırım/fırtına riski, sıcaklık ve zorunlu su molası eşiği, zemin durumu ve rüzgâr; FIFA/FIFPro ve TFF eşiklerine göre."},
    {"path": "/danismanlik/kayak", "page": "danismanlik", "tab": "ski", "label": "Kayak Merkezi",
     "title": "Kayak Merkezi: Kar Yapma ve Çığ Riski | Forecaster",
     "desc": "Kayak merkezleri için kar yapma uygunluğu, meteorolojik çığ riski faktörleri, pist koşulları ve görüş mesafesi."},
    {"path": "/danismanlik/aricilik", "page": "danismanlik", "tab": "beekeeping", "label": "Arıcılık",
     "title": "Arıcılık için Hava Tahmini ve Uçuş Uygunluğu | Forecaster",
     "desc": "Arı uçuş faaliyeti, yağış ve rüzgâr etkisi ve nektar toplama verimliliği; Türkiye arıcılık kaynaklarındaki sıcaklık eşiklerine göre."},
    {"path": "/danismanlik/sel", "page": "danismanlik", "tab": "flood", "label": "Sel Etki Analizi",
     "title": "Sel ve Su Baskını Riski Analizi | Forecaster",
     "desc": "Yağış miktarı, saatlik yağış şiddeti ve nehir debisinin normale oranı birlikte değerlendirilerek önümüzdeki 7 gün için sel ve su baskını riski."},
    {"path": "/danismanlik/erozyon", "page": "danismanlik", "tab": "erosion", "label": "Erozyon Riski",
     "title": "Su ve Rüzgâr Erozyonu Riski | Forecaster",
     "desc": "Yağış şiddeti, ıslak üst toprak, kuvvetli rüzgâr ve kuru toprak koşullarına göre önümüzdeki 7 gün için su ve rüzgâr erozyonu risk göstergesi."},
    {"path": "/danismanlik/iklim", "page": "danismanlik", "tab": "climate", "label": "İklim Projeksiyonu",
     "title": "İklim Projeksiyonu 1991–2050: Sıcaklık ve Yağış | Forecaster",
     "desc": "Seçili konum için 1991–2050 yıllık sıcaklık ve yağış projeksiyonu: çoklu iklim modeli ortalaması, modeller arası aralık ve 2031–2050 değişimi."},
    {"path": "/danismanlik/sigorta", "page": "danismanlik", "tab": "insurance", "label": "Sigorta ve Risk",
     "title": "Sigorta ve Risk Yönetimi için Hava Analizi | Forecaster",
     "desc": "7 günlük hasar riski özeti, son 5 yılın ekstrem hava olayı sıklığı ve belirli bir olay için geçmişe dönük meteorolojik analiz."},
    {"path": "/danismanlik/uzay-havasi", "page": "danismanlik", "tab": "space", "label": "Uzay Havası",
     "title": "Uzay Havası: Kp İndeksi ve Jeomanyetik Fırtına | Forecaster",
     "desc": "Jeomanyetik fırtına (G), radyasyon fırtınası (S) ve radyo kesintisi (R) ölçekleri, Kp indeksi ve güneş rüzgârı; NOAA SWPC verisiyle."},
]

ROUTE_BY_PATH = {r["path"]: r for r in SEO_ROUTES}

# Tarayıcı tarafındaki adres yönetimi bu tabloyu <script id="seo-routes"> ile alır.
_ROUTES_JSON = json.dumps(
    [{k: r[k] for k in ("path", "page", "tab", "title", "desc") if k in r} for r in SEO_ROUTES],
    ensure_ascii=False,
).replace("</", "<\\/")


def _links_html():
    """Sayfanın altındaki link listesi: Google'ın tüm sayfaları bulabilmesi
    için gerçek <a href> linkleri (tıklanınca sayfa yenilenmeden açılır)."""
    def items(rs):
        return "".join(
            f'<li><a href="{r["path"]}" data-route class="text-slate-500 hover:text-brand transition-colors">'
            f'{html.escape(r["label"])}</a></li>'
            for r in rs
        )
    head = 'class="text-slate-400 text-[11px] uppercase tracking-widest font-600 mb-3"'
    ul = 'class="flex flex-wrap gap-x-5 gap-y-2"'
    pages = [r for r in SEO_ROUTES if r["path"].count("/") == 1]   # /, /hava-durumu, /danismanlik …
    tabs = [r for r in SEO_ROUTES if r["path"].count("/") > 1]     # /danismanlik/…
    return (
        '<div class="grid gap-8 md:grid-cols-3">'
        f'<div><p {head}>Sayfalar</p><ul {ul}>{items(pages)}</ul></div>'
        f'<div class="md:col-span-2"><p {head}>Sektörel Danışmanlık</p><ul {ul}>{items(tabs)}</ul></div>'
        "</div>"
    )


_LINKS_HTML = _links_html()
_cache = {"mtime": None, "html": ""}


def _index_html():
    mtime = os.path.getmtime(INDEX_PATH)
    if _cache["mtime"] != mtime:
        with open(INDEX_PATH, encoding="utf-8") as f:
            _cache["html"] = f.read()
        _cache["mtime"] = mtime
    return _cache["html"]


def _set_attr(h, pattern, value):
    """pattern'in 1. ve 2. grubu arasındaki değeri value ile değiştirir."""
    return re.sub(pattern, lambda m: m.group(1) + value + m.group(2), h, count=1)


def render_page(route):
    """index.html'i bu sayfanın başlık, açıklama ve canonical adresiyle döndürür."""
    h = _index_html()
    title = html.escape(route["title"], quote=True)
    desc = html.escape(route["desc"], quote=True)
    url = SITE_URL + route["path"]

    h = re.sub(r"<title>.*?</title>", lambda m: f"<title>{title}</title>", h, count=1, flags=re.S)
    h = _set_attr(h, r'(<meta name="description" content=")[^"]*(")', desc)
    h = _set_attr(h, r'(<meta property="og:title" content=")[^"]*(")', title)
    h = _set_attr(h, r'(<meta property="og:description" content=")[^"]*(")', desc)
    h = _set_attr(h, r'(<meta property="og:url" content=")[^"]*(")', url)
    h = _set_attr(h, r'(<link rel="canonical" href=")[^"]*(")', url)
    h = h.replace("<!--SEO_LINKS-->", _LINKS_HTML, 1)
    h = h.replace(
        "</head>",
        f'<script id="seo-routes" type="application/json">{_ROUTES_JSON}</script>\n</head>',
        1,
    )
    return h


def robots_txt():
    return (
        "User-agent: *\n"
        "Disallow: /admin/\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )


def sitemap_xml():
    urls = "".join(f"  <url><loc>{SITE_URL}{r['path']}</loc></url>\n" for r in SEO_ROUTES)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        "</urlset>\n"
    )
