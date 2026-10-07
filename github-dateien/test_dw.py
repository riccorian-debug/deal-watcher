import sys, os, re, io, copy, contextlib, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deal_watcher as W

fails = 0
def check(name, cond):
    global fails
    print(("OK   " if cond else "FAIL ") + name)
    if not cond: fails += 1

# ---------- Einzelfunktionen ----------
check("preis 320 €", W.parse_price("320 €") == 320)
check("preis 1.299 € VB", W.parse_price("1.299 € VB") == 1299)
check("preis 7,69 €", W.parse_price("7,69 €") == 7.69)
check("preis VB -> None", W.parse_price("VB") is None)
check("fmt 1299", W.fmt_eur(1299) == "1.299 €")
check("fmt 7.69", W.fmt_eur(7.69) == "7,69 €")
check("defekt erkannt", W.mentions_defect("Die Karte ist leider defekt."))
check("nicht defekt ok", not W.mentions_defect("Karte ist nicht defekt, läuft top"))
check("0 defekte Sektoren ok", not W.mentions_defect("SMART: 0 defekte Sektoren, 12000h"))
check("defekte Sektoren: 0 ok", not W.mentions_defect("Reallokierte/defekte Sektoren: 0"))
check("keine Artefakte ok", not W.mentions_defect("keine Artefakte, kein Spulenfiepen"))
check("ungetestet erkannt", W.mentions_defect("Da ungetestet, Verkauf als Bastlerware"))
check("funktioniert nicht erkannt", W.mentions_defect("HDMI funktioniert nicht"))
check("url ohne kat", W.search_url("rtx 3080", None) == "https://www.kleinanzeigen.de/s-rtx-3080/k0?sortingField=SORTING_DATE")
check("url mit kat s2", W.search_url("rtx 3080", "pc-zubehoer", 2) == "https://www.kleinanzeigen.de/s-pc-zubehoer-software/seite:2/rtx-3080/k0c225?sortingField=SORTING_DATE")

cfg, queries = W.load_config()
check(f"config geladen ({len(queries)} Suchen)", len(queries) > 30)
for q in queries:
    for rx in q.get("muss", []) + q.get("nicht", []):
        re.compile(rx)
check("alle Regex kompilieren", True)
qd = {q["name"]: q for q in queries}

def it(title, price, ship=True):
    return {"id": "1", "title": title, "price": price, "gesuch": False, "ship": ship}
q3080 = qd["RTX 3080"]
check("3080 passt", W.matches(it("Gigabyte RTX 3080 Gaming OC 10GB", 320), q3080))
check("3080 Ti raus", not W.matches(it("MSI RTX 3080 Ti Suprim X", 450), q3080))
check("Gaming PC raus", not W.matches(it("Gaming PC Ryzen 5 RTX 3080", 600), q3080))
check("Lot raus", not W.matches(it("3x RTX 3080 aus Mining", 500), q3080))
check("Suche raus", not W.matches(it("Suche RTX 3080", 300), q3080))
check("Kühler raus", not W.matches(it("EK Wasserblock für RTX 3080", 60), q3080))
qram = qd["EliteBook RAM 32GB SO-DIMM"]
check("SO-DIMM 2x16 passt", W.matches(it("Crucial 32GB Kit (2x16GB) DDR4-3200 SO-DIMM", 110), qram))
check("Desktop-RAM raus", not W.matches(it("Corsair 32GB DDR4 3200 Desktop", 80), qram))
check("64GB raus", not W.matches(it("64GB (2x32GB) DDR4 SODIMM", 250), qram))
qiw = qd["Seagate IronWolf 4TB"]
check("IronWolf 4TB passt", W.matches(it("Seagate IronWolf 4TB NAS HDD", 70), qiw))
check("14TB nicht als 4TB", not W.matches(it("Seagate IronWolf 14TB", 180), qiw))
qpi = qd["Raspberry Pi 5 8GB"]
check("Pi5 8GB passt", W.matches(it("Raspberry Pi 5 8GB mit Gehäuse", 120), qpi))
check("Pi4 raus", not W.matches(it("Raspberry Pi 4 8GB", 60), qpi))

# ---------- Fixtures ----------
def article(adid, title, price, ship=True, direct=False, tags=""):
    tag_html = ""
    if ship: tag_html += '<span class="simpletag">Versand möglich</span>'
    if direct: tag_html += '<span class="simpletag">Direkt kaufen</span>'
    return f'''<li class="ad-listitem"><article class="aditem" data-adid="{adid}" data-href="/s-anzeige/x/{adid}-225-1">
<div class="aditem-main"><div class="aditem-main--top"><div class="aditem-main--top--left">44892 Bochum</div>
<div class="aditem-main--top--right">Heute, 11:23</div></div>
<div class="aditem-main--middle"><h2 class="text-module-begin"><a class="ellipsis" href="/s-anzeige/x/{adid}-225-1">{title}</a></h2>
<p class="aditem-main--middle--description">Beschreibung</p>
<div class="aditem-main--middle--price-shipping"><p class="aditem-main--middle--price-shipping--price">{price}
<span class="aditem-main--middle--price-shipping--old-price">999 €</span></p></div></div>
<div class="aditem-main--bottom"><p class="text-module-end">{tag_html}{tags}</p></div></div></article></li>'''

listings = [
 ("100", "Gigabyte RTX 3080 Gaming OC 10GB", "360 €"),
 ("101", "MSI RTX 3080 Gaming X Trio 10GB", "380 € VB"),
 ("102", "ASUS TUF RTX 3080 OC", "370 €"),
 ("103", "Zotac RTX 3080 Trinity", "350 €"),
 ("104", "EVGA RTX 3080 FTW3", "400 €"),
 ("105", "Palit RTX 3080 GamingPro", "345 €"),
 ("106", "RTX 3080 Founders Edition", "390 €"),
 ("107", "Gainward RTX 3080 Phoenix", "365 €"),
 ("200", "Gigabyte RTX 3080 Eagle 10GB", "249 €"),          # Deal, seriöser Verkäufer
 ("201", "RTX 3080 Ventus 3X", "255 €"),                   # Deal, aber Konto 3 Tage alt
 ("202", "RTX 3080 Aorus Master", "260 €"),                # Deal, aber falsche Kategorie
 ("203", "PNY RTX 3080 XLR8", "262 €"),                    # nur Abholung (ohne Versand-Tag)
 ("204", "RTX 3080 Strix", "90 €"),                        # zu billig -> ignorieren
 ("205", "Suche RTX 3080", "300 €"),
 ("206", "3x RTX 3080 aus Mining", "500 €"),
]
page_html = "<html><body><ul>" + "".join(
    article(a, t, p, ship=(a != "203")) for a, t, p in listings) + "</ul></body></html>"

def detail(title, price, since, badges, crumb="Kleinanzeigen > Elektronik > PC-Zubehör & Software > Grafikkarten",
           ship="+ Versand ab 7,69 €", desc="Top Zustand, SMART egal, 0 defekte Pixel.", zustand="Sehr Gut", nutzer="Privater Nutzer",
           attrs=None, with_desc=True):
    b = " ".join(f'<span class="userbadge-tag">{x}</span>' for x in badges)
    attr_html = "".join(f'<li class="addetailslist--detail">{k}<span class="addetailslist--detail--value">{v}</span></li>'
                        for k, v in (attrs or {}).items())
    desc_html = f'<p id="viewad-description-text">{desc}</p>' if with_desc else ""
    return f'''<html><body><div id="vap-brdcrmb" class="breadcrump">{crumb}</div>
<h1 id="viewad-title">{title}</h1><h2 id="viewad-price">{price}</h2><div>{ship}</div>
<ul id="viewad-details"><li class="addetailslist--detail">Zustand <span class="addetailslist--detail--value">{zustand}</span></li>{attr_html}</ul>{desc_html}
<div id="viewad-contact">Nachricht schreiben <span class="userprofile-vip">H.F.</span> {b}
<span>{nutzer}</span> <span>Aktiv seit {since}</span> 3 Anzeigen online</div>
<div>Anzeigen-ID 123</div><div>Andere Anzeigen des Anbieters Versand möglich Nur Abholung TOP Zufriedenheit</div></body></html>'''

details = {
 "200": detail("Gigabyte RTX 3080 Eagle 10GB", "249 €", "09.10.2011", ["TOP Zufriedenheit", "Besonders zuverlässig"]),
 "201": detail("RTX 3080 Ventus 3X", "255 €", (dt.date.today()-dt.timedelta(days=3)).strftime("%d.%m.%Y"), []),
 "202": detail("RTX 3080 Aorus Master", "260 €", "01.01.2020", ["TOP Zufriedenheit"], crumb="Kleinanzeigen > Haus & Garten > Schlafzimmer"),
}

extra_pages = {}

def fake_get(self, url):
    self.count += 1
    m = re.search(r"/s-anzeige/x/(\d+)-", url)
    if m: return details.get(m.group(1))
    if "seite:" in url: return None
    if "rtx-3080" in url: return page_html
    for key, html in extra_pages.items():
        if key in url: return html
    return "<html><body>keine Treffer</body></html>"
W.Fetcher.get = fake_get

parsed = W.parse_search(page_html)
check(f"Suchseite geparst ({len(parsed)} Anzeigen)", len(parsed) == len(listings))
p0 = next(x for x in parsed if x["id"] == "100")
check("alter Preis ignoriert", p0["price"] == 360)
check("Versand erkannt", p0["ship"] and not next(x for x in parsed if x["id"] == "203")["ship"])
d = W.parse_detail(details["200"])
check("Detail: Kontoalter", d["since"] == dt.date(2011, 10, 9))
check("Detail: Siegel nur aus Verkäuferbox", d["badges"] == ["TOP Zufriedenheit", "Besonders zuverlässig"])
check("Detail: Versandkosten", d["ship_cost"] == 7.69 and not d["pickup_only"])
check("Detail: Kategorie", "Elektronik" in d["category"])
check("Detail: Zustand", d["condition"] == "Sehr Gut")

only = [qd["RTX 3080"]]
state = {"gemeldet": {}, "pools": {"RTX 3080": {"seed1": [360, dt.date.today().isoformat(), dt.date.today().isoformat(), "MSI RTX 3080 Gaming X Trio 10GB"]}}}
hits, stats = W.run(cfg, only, state, W.Fetcher())
ids = [h["id"] for h in hits]
print("Treffer:", [(h["id"], h["score"], round(h["discount"])) for h in hits])
check("seriöser Deal gemeldet", ids == ["200"])
check("Bewertung 8/10", hits and hits[0]["score"] == 8)
check("Pool ohne Suche/Lot/zu billig gefiltert", "205" not in state["pools"]["RTX 3080"] and "206" not in state["pools"]["RTX 3080"])
check("gemeldet gespeichert", "200" in state["gemeldet"])

# zweiter Lauf: nichts Neues -> keine Treffer, nur 1 Seite (Pool voll)
before = stats["requests"]
f2 = W.Fetcher()
hits2, stats2 = W.run(cfg, only, state, f2)
check("2. Lauf: keine Doppelmeldung", hits2 == [])
check("2. Lauf: nur 1 Abruf", stats2["requests"] == 1)

# Preissenkung einer bekannten Anzeige -> wird neu bewertet
listings2 = [(a, t, ("240 €" if a == "107" else p)) for a, t, p in listings]
page_html = "<html><body><ul>" + "".join(article(a, t, p, ship=(a != "203")) for a, t, p in listings2) + "</ul></body></html>"
details["107"] = detail("Gainward RTX 3080 Phoenix", "240 €", "05.05.2016", ["Sehr freundlich"], nutzer="Privater Nutzer")
hits3, _ = W.run(cfg, only, state, W.Fetcher())
check("Preissenkung erkannt", [h["id"] for h in hits3] == ["107"])

subj, body = W.compose(hits)
print("\n" + subj + "\n" + body)
check("Betreff", subj.startswith("Deal: Gigabyte RTX 3080 Eagle"))

# Hochpreis-Regel: 180 € ohne Siegel/Käuferschutz -> raus
cand = {"price": 180, "direct": False}
dd = W.parse_detail(detail("x", "180 €", "01.01.2024", []))
mod, notes, reason = W.assess(cand, dd, cfg)
check("ab 150 € ohne Seriosität raus", reason is not None and "150" in reason)
cand["direct"] = True
mod, notes, reason = W.assess(cand, dd, cfg)
check("ab 150 € mit Direkt kaufen ok", reason is None)

# Sperr-Pause: 6 h, bei erneuter Sperre binnen 24 h verdoppeln, max. 48 h
st = {}
W.handle_block(st, {"blocked": "IP-Bereich vorübergehend gesperrt"}, dry=True)
check("1. Sperre -> 6 h Pause", st["zustand"]["pause_h"] == 6)
st["zustand"]["pause_bis"] = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
W.handle_block(st, {"blocked": "HTTP 403"}, dry=True)
check("2. Sperre -> 12 h Pause", st["zustand"]["pause_h"] == 12)
for _ in range(4):
    st["zustand"]["pause_bis"] = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    W.handle_block(st, {"blocked": "HTTP 403"}, dry=True)
check("Pause gedeckelt bei 48 h", st["zustand"]["pause_h"] == 48)
st2 = {"zustand": {"pause_bis": (dt.datetime.now() - dt.timedelta(days=3)).isoformat(timespec="seconds"), "pause_h": 48}}
W.handle_block(st2, {"blocked": "HTTP 403"}, dry=True)
check("Alte Sperre vergessen -> wieder 6 h", st2["zustand"]["pause_h"] == 6)

# Leere Ergebnisse zählen (Layout-Änderung)
st3 = {}
for _ in range(3):
    W.health_check(st3, {"blocked": None, "listings": 0}, dry=True)
check("Leere Läufe gezählt", st3["zustand"]["leer_in_folge"] == 3)


# Bootstrap: neue Suche meldet im ersten Lauf nichts
st_b = {"gemeldet": {}, "pools": {}}
hb, _ = W.run(cfg, only, st_b, W.Fetcher())
check("Erster Lauf je Suche: nur Preisbasis", hb == [] and len(st_b["pools"]["RTX 3080"]) > 5)

# Zahlungsart ohne Kaeuferschutz: PayPal Freunde & Familie ist ein Betrugsmuster -> ausschliessen
dd2 = W.parse_detail(detail("x", "120 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Zahlung per PayPal Freunde oder Überweisung"))
check("unsichere Zahlung erkannt", dd2["unsafe_pay"])
m2, n2, r2 = W.assess({"price": 120, "direct": False, "title": "x"}, dd2, cfg)
check("PayPal Freunde & Familie wird ausgeschlossen", r2 is not None and "zahlung_ohne_schutz" in r2)
dd2b = W.parse_detail(detail("x", "120 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Zahlung per Sicher bezahlen oder PayPal Freunde"))
m2b, n2b, r2b = W.assess({"price": 120, "direct": False, "title": "x"}, dd2b, cfg)
check("mit 'Sicher bezahlen' nur Hinweis statt Ausschluss", r2b is None and any("Hinweis" in x for x in n2b) and m2b < 0)
dd2c = W.parse_detail(detail("x", "120 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Zahlung per Überweisung nach Absprache, Rechnung liegt bei"))
m2c, n2c, r2c = W.assess({"price": 120, "direct": False, "title": "x"}, dd2c, cfg)
check("nur Überweisung: Hinweis auf Käuferschutz", r2c is None and any("Käuferschutz" in x for x in n2c))

# Detailseite zeigt Fantasiepreis (5 €) -> nicht melden
details["200"] = detail("Gigabyte RTX 3080 Eagle 10GB", "5 €", "09.10.2011", ["TOP Zufriedenheit", "Besonders zuverlässig"])
st_p = {"gemeldet": {}, "pools": {"RTX 3080": {"s1": [360, dt.date.today().isoformat(), dt.date.today().isoformat(), "MSI RTX 3080 Gaming X Trio 10GB"]}}}
hp, _ = W.run(cfg, only, st_p, W.Fetcher())
check("Fantasiepreis auf Detailseite verworfen", "200" not in [h["id"] for h in hp])
# Pool wird mit aktuellen Filtern nachgefiltert / alte Eintraege ohne Titel -> Neuaufbau
st_r = {"gemeldet": {}, "pools": {"RTX 3080": {"a": [300, dt.date.today().isoformat(), dt.date.today().isoformat()]}}}
hr, _ = W.run(cfg, only, st_r, W.Fetcher())
check("Alter Pool ohne Titel -> nur Preisbasis", hr == [] and all(len(v) == 4 for v in st_r["pools"]["RTX 3080"].values()))
st_f = {"gemeldet": {}, "pools": {"RTX 3080": {"b": [300, dt.date.today().isoformat(), dt.date.today().isoformat(), "Suche RTX 3080"]}}}
W.run(cfg, only, st_f, W.Fetcher())
check("Pool nachgefiltert", "b" not in st_f["pools"]["RTX 3080"])
check("Laptop ohne SSD raus", W.why_not({"title": "ThinkPad T14 Gen 2 i5 16GB keine SSD", "price": 275, "gesuch": False}, {"kategorie": "notebooks"}) is not None)

dg = W.parse_detail(detail("Grafikkarte GeForce RTX 4060 Ti 16GB", "5 €", "01.01.2024", [], desc="Suche eine RTX 4060 Ti 16GB, biete bis 300 Euro."))
check("Gesuch auf Detailseite erkannt", dg["gesuch"] and W.assess({"price": 5, "direct": False}, dg, cfg)[2] is not None)
dn = W.parse_detail(detail("RTX 4060 Ti 16GB", "300 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Verkaufe meine Karte, läuft einwandfrei."))
check("normales Angebot kein Gesuch", not dn["gesuch"])


# =====================================================================================
#  NEU: Maengel-Scanner, Marken-Stufen, Fake-Grenzen, Beschreibung lesen, --explain
# =====================================================================================
HARD_CASES = [
    "Die Karte ist leider defekt.",
    "Display hat einen Sprung",
    "Akku hält nur 20 Minuten",
    "Scharnier links lose",
    "BIOS-Passwort leider vergessen",
    "lief bis gestern, geht nicht mehr an",
    "Das native 12vHpwr Kabel ist leider beschädigt und somit nicht mehr zu gebrauchen",
    "SSD-Gesundheit 74 %",
    "Der Laptop kommt ohne Netzteil",
    "Wasserschaden, Laptop startet nicht",
    "Da ungetestet, Verkauf als Bastlerware",
    "iCloud gesperrt, Aktivierungssperre aktiv",
    "HDMI funktioniert nicht",
    "Tastatur defekt, einzelne Tasten fehlen",
    "Lüfter ist laut und rattert",
    "Display gebrochen, unten links ein Riss",
    "Pixelfehler in der Mitte des Bildschirms",
    "Gerät schaltet sich plötzlich ab",
    "Bluescreen beim Starten",
    "kein Bild, schwarzer Bildschirm",
    "Verkaufe als Ersatzteilspender",
    "Mainboard defekt, Rest funktioniert",
    "Akku ist aufgebläht",
    "Festplatte klickt beim Start",
    "SMART-Warnung vorhanden, 12 reallocated sectors",
    "Reallocated Sectors: 5",
    "Defekte Sektoren: 8",
    "Netzteil nicht dabei",
    "Ladegerät fehlt",
    "Zustand: SSD und RAM fehlen",
    "nur Gehäuse ohne Inhalt",
    "Funktioniert nicht richtig, hängt sich auf",
    "USB-C Port wackelt und ist lose",
    "Touchpad reagiert nicht mehr",
    "Laptop ist nicht funktionsfähig",
    "Gerät ist gesperrt (Firmensperre / MDM)",
    "Supervisor-Passwort gesetzt",
    "Abstürze unter Last, wird sehr heiß",
    "Die Grafikkarte hat Artefakte im Bild",
    "Lüfter schleift",
    "Ein Fuß ist abgebrochen",
    "Kaffee über die Tastatur verschüttet",
    "Sturzschaden am Gehäuse",
    "Bildschirm flackert",
    "Streifen im Display",
    "Nutzerkonto Passwort unbekannt",
    "Als gestohlen gemeldet",
    "Akku Gesundheit nur 55 %",
    "Zahlung nur per PayPal Freunde und Familie",
    "Bitte schreib mir per WhatsApp 0171 2345678",
    "Preis 150 Euro, nur Vorkasse per Überweisung vorab",
    "Das Display hat leider Einbrennen",
    "Teilweise defekt: linker Lautsprecher",
    "Läuft nicht stabil",
    "Funktioniert nur noch teilweise",
    "Laptop muss repariert werden",
    "Reparaturbedürftig",
    "Wackelkontakt am Ladeanschluss",
    "bootet nicht mehr",
    "Ohne SSD, ohne RAM",
    "Zustand nicht getestet",
    "konnte ich nicht testen",
]
SOFT_CASES = [
    "Habe das Display reparieren lassen, seitdem alles gut",
    "Leichte Geräusche beim Lüfter",
    "Fleck auf dem Gehäuse",
    "Probleme gab es bisher keine, aber Fehlermeldung beim ersten Start",
    "Aus Haushaltsauflösung, Zustand unbekannt",
    "Backlight Bleeding in der Ecke",
    "Gerät aus Retoure",
    "keine Ahnung ob alles geht",
    "Spulenfiepen leicht hörbar",
    "Akku nur 78 %",
    "SSD Gesundheit 85 %",
    "Aufkleberreste auf dem Deckel",
]
CLEAN_CASES = [
    "Karte ist nicht defekt, läuft top",
    "SMART: 0 defekte Sektoren, 12000h",
    "Reallokierte/defekte Sektoren: 0",
    "Defekte Sektoren: 0",
    "keine Artefakte, kein Spulenfiepen",
    "Keine Mängel, nur leichte Gebrauchsspuren",
    "Rückgabe bei Defekt möglich",
    "Privatverkauf unter Ausschluss jeglicher Gewährleistung und Sachmängelhaftung. Keine Rücknahme oder Garantie.",
    "Netzteil wird nicht benötigt",
    "Einwandfrei, getestet und funktionsfähig",
    "kein BIOS-Passwort, nicht gesperrt",
    "ohne Kratzer, Akku 94 %",
    "Akku 100 % geladen",
    "Wasserschaden ausgeschlossen",
    "Wasserschaden: nein",
    "Alles funktioniert, nichts defekt",
    "Gerät hat keinerlei Defekte",
    "Display ohne Pixelfehler",
    "frei von Mängeln",
    "Falls defekt, nehme ich es zurück",
    "Garantie bei Defekt über den Hersteller bis 2027",
    "Windows 11 Pro neu installiert, Akku 56 Ladezyklen, Original-Netzteil dabei",
    "Der Laptop läuft einwandfrei, Tastatur und Display ohne Fehler",
    "Läuft nicht heiß und ist leise",
    "SSD-Gesundheit 100 %",
    "Original Ladegerät und Karton dabei, Kratzer am Deckel",
    "Versand als versichertes Paket, Zahlung per PayPal Waren und Dienstleistungen",
    "Zahlung bitte über Sicher bezahlen, kein PayPal Freunde",
    "Top gepflegt, Nichtraucherhaushalt, Rechnung vorhanden",
    "Kein Wasserschaden, kein Sturzschaden, keine Reparatur",
    "Bei Fragen gerne melden. Keine Probleme mit dem Display",
    "Defekte Pixel: keine",
    "wurde nie repariert",
    "Das Display ist makellos, ohne Sprung und ohne Riss",
    "Garantie auf Defekte ausgeschlossen",
    "Zustand sehr gut, Akku hält über 8 Stunden",
    "Lüfter ist leise",
]
for txt in HARD_CASES:
    check(f"Mangel HARD: {txt[:55]}", W.scan_defects("", txt)["level"] == W.HARD)
for txt in SOFT_CASES:
    check(f"Hinweis (SOFT/HARD): {txt[:50]}", W.scan_defects("", txt)["level"] in (W.SOFT, W.HARD))
for txt in CLEAN_CASES:
    r = W.scan_defects("", txt)
    check(f"kein Mangel: {txt[:55]}", r["level"] == W.CLEAN)
check("Zustandsfeld 'Defekt' -> HARD", W.scan_defects("Karte", "Läuft top", "Defekt")["level"] == W.HARD)
check("Mangel im Titel -> HARD", W.scan_defects("ThinkPad T14 Display gebrochen", "")["level"] == W.HARD)
check("Mangel am ENDE einer langen Beschreibung gefunden",
      W.scan_defects("Laptop", ("Sehr gepflegt, Windows neu installiert. " * 40) + "PS: Das Display hat leider einen Sprung.")["level"] == W.HARD)
check("Mangel in späterer Zeile (Zeilenumbruch) gefunden", W.scan_defects("x", "Alles top\nAkku ist tot\nVersand möglich")["level"] == W.HARD)
check("Beschreibung zu kurz bei teurer Ware -> SOFT", W.scan_defects("Laptop", "Gut", price=300)["level"] == W.SOFT)
check("Beschreibung = Titel -> SOFT", W.scan_defects("HGST 12TB Festplatte", "HGST 12TB Festplatte", price=50)["level"] == W.SOFT)
check("Treffer nennen den Satz", "Sprung" in W.scan_defects("", "Display hat einen Sprung")["hits"][0]["snippet"])
check("NAS ohne Festplatte normal (ignoriere)", W.scan_defects("", "Synology ohne Festplatte", ignore=("unvollstaendig_speicher",))["level"] == W.CLEAN)
check("... aber ohne ignoriere ein Mangel", W.scan_defects("", "Laptop ohne Festplatte")["level"] == W.HARD)
check("Ausstattungsstufen sind kein Mangel (nur Hinweis)",
      W.scan_defects("", "159€ ohne RAM/SSD, 195€ mit 8GB RAM und 256GB SSD")["level"] == W.SOFT)
check("mentions_defect bleibt kompatibel", W.mentions_defect("HDMI funktioniert nicht") and not W.mentions_defect("kein Defekt"))

# ---------- Titel-Pruefung bei der Suche ----------
q_nas = {"name": "n", "gruppe": "nas", "muss": [], "nicht": []}
q_gpu = {"name": "g", "gruppe": "gpu", "muss": [], "nicht": []}
q_ssd = {"name": "s", "gruppe": "ssd", "muss": [], "nicht": []}
def itx(title, price): return {"id": "1", "title": title, "price": price, "gesuch": False, "ship": True}
check("Titel: Display gebrochen raus", W.why_not(itx("ThinkPad T14 Display gebrochen", 200), {"name": "x"}) is not None)
check("Titel: Laptop ohne Netzteil raus", W.why_not(itx("ThinkPad T14 i5 ohne Netzteil", 200), {"name": "x"}) is not None)
check("Titel: NAS ohne Festplatte ok", W.why_not(itx("Synology DS220+ ohne Festplatte", 180), q_nas) is None)
check("Titel: GPU ohne Kühler raus", W.why_not(itx("RTX 3080 ohne Kühler", 200), q_gpu) is not None)

# ---------- Fake-Mindestpreise ----------
check("Fake-Grenze: 2TB SSD für 30 € raus", "Fake" in (W.why_not(itx("Samsung 990 Pro 2TB NVMe", 30), q_ssd) or ""))
check("2TB SSD für 99 € ok", W.why_not(itx("Samsung 990 Pro 2TB NVMe", 99), q_ssd) is None)
check("Fake-Grenze: RTX 4070 für 90 € raus", W.price_floor_hit(q_gpu, "Gigabyte RTX 4070", 90) == 200)
check("Fake-Grenze: 12TB HDD für 40 € raus", W.price_floor_hit({"gruppe": "hdd"}, "Seagate IronWolf 12TB", 40) == 65)
check("Fake-Grenze: 4TB HDD für 45 € ok", W.price_floor_hit({"gruppe": "hdd"}, "Seagate IronWolf 4TB", 45) is None)
check("Fake-Grenze: 32GB RAM für 20 € raus", W.price_floor_hit({"gruppe": "ram"}, "Crucial 32GB DDR4 SO-DIMM", 20) == 40)
check("2x16GB nicht als 16GB-Boden missverstanden", W.price_floor_hit({"gruppe": "ram"}, "Kit 32GB (2x16GB) DDR4", 80) is None)

# ---------- Marken und Stufen ----------
BRAND_CASES = [
    ("gpu", "Gigabyte RTX 3080 Eagle 10GB", "A"), ("gpu", "RTX 3080 Ventus 3X", "A"), ("gpu", "Zotac RTX 4070 Twin Edge", "A"),
    ("gpu", "Maxsun RTX 3060 12GB", "C"), ("gpu", "Soyo RX 580 8GB", "C"), ("gpu", "Colorful RTX 3070", "A"),
    ("ram", "Crucial 32GB Kit (2x16GB) DDR4-3200 SO-DIMM", "A"), ("ram", "SK Hynix 2x16GB DDR4 SO-DIMM", "A"),
    ("ram", "Samsung 32GB DDR4 3200 SODIMM", "A"), ("ram", "Kingston Fury 32GB DDR5 6000", "A"),
    ("ram", "Ramaxel 32GB DDR4 SO-DIMM", "B"), ("ram", "Lexar 16GB DDR4 3200", "B"), ("ram", "32GB (2x16) DDR4 3200 SO-DIMM Laptop", "B"),
    ("ram", "Fanxiang 32GB DDR4 3200 SODIMM", "C"), ("ram", "KingSpec 16GB DDR4 SO-DIMM", "C"), ("ram", "Walram 32GB DDR4", "C"),
    ("ssd", "Samsung 990 Pro 2TB NVMe", "A"), ("ssd", "WD Black SN850X 2TB", "A"), ("ssd", "Kingston NV2 1TB", "A"),
    ("ssd", "Lexar NM790 2TB", "B"), ("ssd", "Gigabyte Aorus 2TB NVMe", "B"),
    ("ssd", "KingSpec 2TB NVMe SSD", "C"), ("ssd", "Fanxiang S660 2TB", "C"), ("ssd", "2TB NVMe SSD M.2 Gen4", "C"),
    ("hdd", "Seagate IronWolf 4TB NAS HDD", "A"), ("hdd", "Toshiba MG07 14TB", "A"), ("hdd", "WD Red Plus 8TB", "A"),
    ("hdd", "Netac 4TB HDD", "C"), ("hdd", "Intenso 4TB Festplatte", "B"),
    ("nas", "Synology DS920+", "A"), ("nas", "QNAP TS-453D", "A"), ("nas", "Terramaster F2-212", "B"),
    ("laptop", "Lenovo ThinkPad T14 Gen 2 i5", "A"), ("laptop", "Lenovo L14 Gen 3 Ryzen 7", "A"), ("laptop", "HP EliteBook 840 G8", "A"),
    ("laptop", "Dell Latitude 7420", "A"), ("laptop", "Apple MacBook Air M1", "A"), ("laptop", "MacBook Air M2", "A"),
    ("laptop", "Lenovo IdeaPad 3 15", "B"), ("laptop", "HP Pavilion 15", "B"), ("laptop", "Acer Aspire 5", "B"), ("laptop", "HP ProBook 450 G8", "B"),
    ("laptop", "Chuwi HeroBook Pro", "C"), ("laptop", "Laptop 15,6 Zoll i5 16GB", "C"),
    ("minipc", "Lenovo ThinkCentre M920q Tiny i5", "A"), ("minipc", "HP EliteDesk 800 G6 Mini", "A"), ("minipc", "Beelink SER5", "B"),
    ("minipc", "NiPoGi Mini PC N100", "C"), ("minipc", "Mini PC Intel N100 16GB", "C"),
    ("cpu", "AMD Ryzen 7 7800X3D", "A"), ("cpu", "Intel Core i7-12700K", "A"), ("cpu", "Intel i7-12700K ES", "C"),
    ("server", "HP ProLiant MicroServer Gen10 Plus", "A"), ("server", "Fujitsu Primergy TX1320 M4", "A"),
    ("psu", "Corsair RM850x", "A"), ("psu", "be quiet! Straight Power 850W", "A"), ("psu", "Segotep 750W", "C"),
]
for gr, title, tier in BRAND_CASES:
    got = W.detect_brand(gr, title)[1]
    check(f"Marke/Stufe {gr}: {title[:40]} -> {tier}", got == tier)
check("Marke aus Attribut bei unbekanntem Titel", W.detect_brand("ssd", "2TB NVMe SSD", "Samsung")[1] == "A")
check("Marken-Label zeigt Marke statt Modell", W.detect_brand("gpu", "Gigabyte RTX 3080 Eagle")[0] == "gigabyte")

# ---------- Preise nur innerhalb derselben Stufe vergleichen ----------
today_s = dt.date.today().isoformat()
def seed(pool_items, start=1000):
    return {str(start + i): [p, today_s, today_s, t] for i, (t, p) in enumerate(pool_items)}
q_ssd2 = {"name": "T SSD 2TB", "begriff": "ssd 2tb", "kategorie": "pc-zubehoer", "gruppe": "ssd",
          "muss": [r"\b2\s*tb"], "preis_min": 30, "preis_max": 400}
A_items = [(f"Samsung 990 Pro 2TB NVMe #{i}", 150 + i * 4) for i in range(8)]            # Markenware ~165 €
C_items = [(f"Fanxiang S660 2TB NVMe #{i}", 70 + i * 2) for i in range(8)]               # China-Billigware ~77 €
pool_mix = seed(A_items + C_items)
cand_c = {"id": "9001", "title": "Fanxiang 2TB NVMe SSD M.2", "price": 55, "ship": True, "direct": False, "gesuch": False, "vb": False, "url": "u", "ort": "", "datum": ""}
cand_a = {"id": "9002", "title": "Samsung 990 Pro 2TB NVMe", "price": 99, "ship": True, "direct": False, "gesuch": False, "vb": False, "url": "u", "ort": "", "datum": ""}
cand_unk = {"id": "9003", "title": "2TB NVMe SSD M.2 Gen4", "price": 60, "ship": True, "direct": False, "gesuch": False, "vb": False, "url": "u", "ort": "", "datum": ""}
c1, why1 = W.qualify_why(cand_c, pool_mix, q_ssd2, cfg)
check("Billigware (Stufe C) wird NICHT gemeldet, obwohl 66 % unter Markenware", c1 is None and "Stufe C" in (why1 or ""))
c2, why2 = W.qualify_why(cand_a, pool_mix, q_ssd2, cfg)
check("Markenware-Schnäppchen (Stufe A) wird gemeldet", c2 is not None and c2["tier"] == "A" and c2["brand"] == "samsung")
check("Marktpreis kommt nur aus Stufe A (~165 €, nicht aus dem Mischtopf)", c2 and 155 <= c2["median"] <= 175 and c2["n"] == 8)
c3, why3 = W.qualify_why(cand_unk, pool_mix, q_ssd2, cfg)
check("Unbekannte Marke bei SSD = Stufe C, nicht gemeldet", c3 is None and "Stufe C" in (why3 or ""))
cfg_nn = {**cfg, "noname_melden": True}
cand_c2 = {**cand_c, "price": 48}
c4, why4 = W.qualify_why(cand_c2, pool_mix, q_ssd2, cfg_nn)
check("noname_melden=true: Stufe C nur gegen Stufe C verglichen", c4 is not None and c4["n"] == 8 and 70 <= c4["median"] <= 85)
# Stufe B ohne genug B-Vergleiche -> mit A-Preisen, aber 15 % Abschlag
q_ram2 = {"name": "T RAM", "begriff": "ram", "gruppe": "ram", "muss": [], "preis_min": 20, "preis_max": 400}
pool_ram = seed([(f"Crucial 32GB DDR4 3200 SO-DIMM #{i}", 150 + i) for i in range(8)] + [("Ramaxel 32GB DDR4 SO-DIMM", 90)])
cand_b = {"id": "9100", "title": "Ramaxel 32GB DDR4 3200 SO-DIMM", "price": 95, "ship": True, "direct": False, "gesuch": False, "vb": False, "url": "u", "ort": "", "datum": ""}
cb, whyb = W.qualify_why(cand_b, pool_ram, q_ram2, cfg)
check("Stufe B ohne B-Vergleiche: A-Preis mit Abschlag", cb is not None and cb["tier"] == "B" and 120 <= cb["median"] <= 135 and "Abschlag" in cb["tier_note"])
cfg_nb = {**cfg, "b_mit_a_vergleichen": False}
check("... ausschaltbar", W.qualify_why(cand_b, pool_ram, q_ram2, cfg_nb)[0] is None)
cand_a2 = {**cand_b, "id": "9101", "title": "Crucial 32GB DDR4 3200 SO-DIMM", "price": 100}
check("Stufe A mit zu wenigen A-Vergleichen: nichts", W.qualify_why(cand_a2, seed([("Crucial 32GB DDR4 3200", 150)] * 3), q_ram2, cfg)[0] is None)
q_plain = {"name": "T ohne Gruppe", "muss": [], "preis_min": 1, "preis_max": 999}
check("Suche ohne Gruppe verhält sich wie früher (ein Topf)",
      W.qualify_why(cand_a, seed([(f"Irgendwas {i}", 150) for i in range(8)]), q_plain, cfg)[0] is not None)

# ---------- Komplettlauf: Marken-Stufen + Beschreibung ----------
ssd_listings = [(str(3000 + i), f"Samsung 990 Pro 2TB NVMe Variante {i}", f"{150 + i * 4} €") for i in range(8)] + [
    ("4001", "Samsung 990 Pro 2TB NVMe neu", "99 €"),           # Markenware-Deal, saubere Beschreibung
    ("4002", "Samsung 990 Pro 2TB NVMe Sammlerstück", "101 €"),   # Markenware-Deal, aber Mangel am Ende der Beschreibung
    ("4003", "Fanxiang 2TB NVMe SSD M.2", "55 €"),                # Billigware
    ("4004", "Samsung 990 Pro 2TB NVMe Spezial", "103 €"),        # Beschreibung nicht lesbar
    ("4005", "Samsung 990 Pro 2TB NVMe Pro", "97 €"),             # Anzeige-Marke widerspricht dem Titel
]
extra_pages["samsung-990-pro-2tb"] = "<html><body><ul>" + "".join(article(a, t, p) for a, t, p in ssd_listings) + "</ul></body></html>"
GOOD = "Neu und originalverpackt, Rechnung liegt bei. Versand versichert. Keine Mängel."
details["4001"] = detail("Samsung 990 Pro 2TB NVMe neu", "99 €", "09.10.2011", ["TOP Zufriedenheit", "Besonders zuverlässig"], desc=GOOD,
                         attrs={"Marke": "Samsung"})
details["4002"] = detail("Samsung 990 Pro 2TB NVMe Sammlerstück", "101 €", "09.10.2011", ["TOP Zufriedenheit"],
                         desc=("Sehr schöne SSD, original verpackt, Versand möglich. " * 10) + "PS: Die SSD hat leider defekte Sektoren: 12.")
details["4003"] = detail("Fanxiang 2TB NVMe SSD M.2", "55 €", "09.10.2011", ["TOP Zufriedenheit"], desc=GOOD)
details["4004"] = detail("Samsung 990 Pro 2TB NVMe Spezial", "103 €", "09.10.2011", ["TOP Zufriedenheit"], desc="egal", with_desc=False)
details["4005"] = detail("Samsung 990 Pro 2TB NVMe Pro", "97 €", "09.10.2011", ["TOP Zufriedenheit"], desc=GOOD, attrs={"Marke": "Fanxiang"})
q_run = {**q_ssd2, "begriff": "samsung 990 pro 2tb", "name": "SSD Lauf", "muss": [r"\b2\s*tb"]}
state_s = {"gemeldet": {}, "pools": {"SSD Lauf": {str(3000 + i): [150 + i * 4, today_s, today_s, f"Samsung 990 Pro 2TB NVMe Variante {i}"] for i in range(8)}}}
log = io.StringIO()
with contextlib.redirect_stdout(log):
    hits_s, st_s = W.run(cfg, [q_run], state_s, W.Fetcher())
out = log.getvalue()
print(out)
ids_s = [h["id"] for h in hits_s]
check("Komplettlauf: nur der saubere Markenware-Deal gemeldet", ids_s == ["4001"])
check("Komplettlauf: Mangel am Ende der Beschreibung erkannt", "Mangelhinweis" in out and "4002" not in ids_s)
check("Komplettlauf: Billigware nie gemeldet", "4003" not in ids_s)
check("Komplettlauf: nicht lesbare Beschreibung -> nicht gemeldet", "Beschreibung nicht lesbar" in out and "4004" not in ids_s)
check("Komplettlauf: widersprüchliche Marke -> nicht gemeldet", "Stufe C" in out and "4005" not in ids_s)
check("Komplettlauf: Zähler 'Beschreibung nicht lesbar'", st_s["detail_unreadable"] == 1 and st_s["detail_checked"] >= 3)
h0 = hits_s[0]
check("Treffer trägt Marke, Stufe und Beschreibungs-Nachweis", h0["brand"] == "samsung" and h0["tier"] == "A" and h0["desc_len"] == len(GOOD) and h0["scan_level"] == W.CLEAN)
subj2, body2 = W.compose(hits_s)
check("Mail: Marke/Stufe", "Marke: samsung (Stufe A" in body2)
check("Mail: Beschreibung gelesen + Zitat", f"Beschreibung gelesen ({len(GOOD)} Zeichen): keine Mängelhinweise gefunden" in body2 and "Rechnung liegt bei" in body2)

# ---------- Detailseite: Attribute, Modellkonflikt, SOFT-Abzug ----------
dA = W.parse_detail(detail("Gigabyte RTX 3070", "300 €", "01.01.2015", ["TOP Zufriedenheit"], desc=GOOD, attrs={"Modell": "RTX 3060", "Marke": "Gigabyte"}))
check("Attribute gelesen", dA["attrs"].get("Marke") == "Gigabyte" and dA["attrs"].get("Zustand") == "Sehr Gut")
check("Beschreibung gefunden", dA["desc_found"] and dA["desc"] == GOOD)
_, _, rA = W.assess({"price": 300, "direct": True, "title": "Gigabyte RTX 3070"}, dA, cfg, q_gpu)
check("GPU-Modell widerspricht Attribut -> ausgeschlossen", rA is not None and "widerspricht" in rA)
dS = W.parse_detail(detail("Laptop", "300 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Gerät aus Haushaltsauflösung, sonst alles gut, Versand möglich."))
mS, nS, rS = W.assess({"price": 300, "direct": True, "title": "Laptop"}, dS, cfg)
dS0 = W.parse_detail(detail("Laptop", "300 €", "01.01.2015", ["TOP Zufriedenheit"], desc="Gerät in sehr gutem Zustand, Versand möglich."))
mS0, _, _ = W.assess({"price": 300, "direct": True, "title": "Laptop"}, dS0, cfg)
check("SOFT-Hinweis: kein Ausschluss, aber Abzug und Notiz", rS is None and mS == mS0 - 2 and any("Hinweis" in x for x in nS))
dN = W.parse_detail(detail("Laptop", "300 €", "01.01.2015", ["TOP Zufriedenheit"], with_desc=False))
check("Beschreibung fehlt -> nicht lesbar", not dN["desc_found"] and W.assess({"price": 300, "direct": True, "title": "Laptop"}, dN, cfg)[2].startswith("Beschreibung nicht lesbar"))

# ---------- --explain ----------
html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_explain_test.html")
with open(html_path, "w", encoding="utf-8") as f:
    f.write(detail("Samsung 990 Pro 2TB NVMe", "99 €", "09.10.2011", ["TOP Zufriedenheit"],
                   desc="Läuft top, aber der Akku hält nicht lange und die SSD hat leider einen Sprung im Gehäuse.", attrs={"Marke": "Samsung"}))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.explain("x", html_path, "ssd")
os.remove(html_path)
ex = buf.getvalue()
check("--explain: Marke und Stufe", "Stufe A" in ex and "samsung" in ex)
check("--explain: Scanner zeigt Mangel mit Satz", "Mängel-Scanner: HARD" in ex and "Sprung" in ex)
check("--explain: Beschreibungslänge", "Beschreibung: gefunden" in ex)

# ---------- Gesundheitscheck: Anzeigenseiten nicht lesbar ----------
st4 = {}
W.health_check(st4, {"blocked": None, "listings": 50, "detail_checked": 4, "detail_unreadable": 4}, dry=False)
check("Alle Detailseiten unlesbar -> Warnung ausgelöst", bool(st4["zustand"]["letzte_warnung"]))
st5 = {}
W.health_check(st5, {"blocked": None, "listings": 50, "detail_checked": 4, "detail_unreadable": 1}, dry=False)
check("Einzelne unlesbare Seiten lösen keine Warnung aus", not st5["zustand"]["letzte_warnung"])

# ---------- marken.toml ----------
mk = W.get_marken()
check("marken.toml: alle Gruppen aus config.toml vorhanden", all(q.get("gruppe") in mk["gruppe"] for q in queries if q.get("gruppe")))
check("marken.toml: Fake-Grenzen geladen", len(mk["boden"]) >= 20)
check("config.toml: jede Suche außer Raspberry Pi hat eine Gruppe", [q["name"] for q in queries if not q.get("gruppe")] == ["Raspberry Pi 5 8GB", "Raspberry Pi 5 16GB"])

print("\nFEHLER:", fails)
sys.exit(1 if fails else 0)
