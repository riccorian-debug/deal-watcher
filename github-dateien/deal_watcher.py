#!/usr/bin/env python3
"""
Deal-Watcher – findet Technik-Angebote auf kleinanzeigen.de deutlich unter Marktpreis.

Laeuft komplett ohne KI (keine Tokens). Pro Suchbegriff werden die neuesten Anzeigen
geholt; daraus entsteht mit der Zeit ein Marktpreis (Median aller gesehenen Anzeigen
der letzten Wochen). Neue Anzeigen oder Preissenkungen, die klar darunter liegen,
werden auf der Anzeigenseite geprueft (Verkaeuferkonto, Bewertungen, Kategorie,
Zustand, Versand) und mit einer 1-10 Bewertung per Mail gemeldet.

Aufruf:
  python deal_watcher.py              normaler Lauf
  python deal_watcher.py --dry-run    ohne Mail, ohne state.json zu speichern (zum Testen)
  python deal_watcher.py --test-mail  schickt nur eine Testmail (prueft die Zugangsdaten)
  python deal_watcher.py --explain 3532125921           zeigt, wie eine einzelne Anzeige bewertet wird
  python deal_watcher.py --explain URL --gruppe ssd     (Marke/Stufe fuer eine Produktgruppe)
  python deal_watcher.py --explain x --html seite.html  (gespeicherte Anzeigenseite statt Abruf)

Einstellungen und Suchbegriffe: config.toml. Marken und Qualitaetsstufen: marken.toml.
Einrichtung: ANLEITUNG.md.

Sicherheitsnetz gegen Fehlmeldungen (alles ohne KI):
  * Maengel-Scanner: Titel, Zustand und die komplette Beschreibung werden satzweise auf Defekte,
    Schaeden, Sperren, fehlende Teile, schlechte Gesundheitswerte und Betrugsmuster geprueft.
  * Marken-Stufen A/B/C: Markenware wird nie mit No-Name-/China-Billigware verglichen; Stufe C
    wird standardmaessig gar nicht gemeldet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import random
import re
import smtplib
import statistics
import sys
import time
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("Python 3.11 oder neuer wird benoetigt (tomllib fehlt).")

BASE = "https://www.kleinanzeigen.de"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.toml")
MARKEN_PATH = os.path.join(HERE, "marken.toml")
STATE_PATH = os.path.join(HERE, "state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Kurzname in config.toml -> (URL-Teil, Kategorie-ID) auf kleinanzeigen.de
CATEGORIES = {
    "pc-zubehoer": ("pc-zubehoer-software", 225),
    "pcs": ("pcs", 228),
    "notebooks": ("notebooks", 278),
    "elektronik": ("multimedia-elektronik", 161),
}

DEFAULTS = {
    "min_rabatt_prozent": 20,        # mind. so viel % unter Marktpreis
    "min_spielraum_eur": 20,         # mind. so viel EUR Luft fuer Weiterverkauf
    "zu_billig_prozent": 35,         # unter X % vom Marktpreis = vermutlich falscher Artikel/Fake
    "min_vergleiche": 6,             # so viele Vergleichsanzeigen braucht ein Marktpreis
    "startseiten": 3,                # Seiten beim ersten Aufbau der Preisbasis (max. 5)
    "pool_tage": 30,                 # wie lange Vergleichspreise gueltig bleiben
    "versand_schaetzung_eur": 7.0,   # falls Versandkosten unbekannt
    "nur_mit_versand": True,
    "min_bewertung": 4,
    "max_detailpruefungen": 12,
    "max_treffer_pro_mail": 10,
    "sperr_konto_tage": 14,          # juengere Konten werden ignoriert
    "junges_konto_tage": 180,        # juengere Konten: -2 Punkte
    "serioes_konto_tage": 365,       # seriöser Verkaeufer: so alt + positives Siegel
    "top_konto_tage": 730,           # +1 Punkt ab diesem Alter + positives Siegel
    "hochpreis_eur": 150,            # ab hier nur mit seriösem Verkaeufer oder Kaeuferschutz
    "noname_melden": False,          # Stufe C (No-Name/China-Billigware, Marke unbekannt) melden? Standard: nein
    "b_mit_a_vergleichen": True,     # Stufe B darf mit Stufe-A-Preisen verglichen werden, wenn B zu wenige Vergleiche hat
    "b_abschlag_prozent": 15,        # ... dann aber mit diesem Abschlag auf den Stufe-A-Marktpreis
    "soft_abzug": 2,                 # Punkte Abzug bei unklaren Hinweisen in der Beschreibung
    "pause_min_s": 1.5,
    "pause_max_s": 3.5,
}

POSITIVE_BADGES = ["TOP Zufriedenheit", "Besonders freundlich", "Sehr freundlich",
                   "Besonders zuverlässig", "Sehr zuverlässig"]
ALL_BADGES = POSITIVE_BADGES + ["OK Zufriedenheit", "Na ja Zufriedenheit", "Freundlich", "Zuverlässig"]

TITLE_EXCLUDE = re.compile(
    r"defekt|kaputt|bastler|für\s+teile|\bsuche\b|\bsuchen\b|gesucht|\btausch|ankauf|wir\s+kaufen|"
    r"reserviert|verkauft\b", re.I)
LOT_RE = re.compile(r"(^|\s)\d+\s*[x×]\s|\b\d+\s*(stück|stk)\b|konvolut|sammlung", re.I)
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*€")


class Blocked(Exception):
    """kleinanzeigen verweigert den Zugriff (403/429/Captcha)."""


# --------------------------------------------------------------------------- Hilfen

def today() -> dt.date:
    return dt.date.today()


def parse_price(text: str | None) -> float | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    whole = m.group(1).replace(".", "")
    cents = m.group(2) or "0"
    return float(f"{whole}.{cents}")


def fmt_eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", ".") if v >= 100 or v == int(v) else f"{v:.2f} €".replace(".", ",")


def trimmed_median(values: list[float]) -> float:
    v = sorted(values)
    k = int(len(v) * 0.1) if len(v) >= 10 else 0
    if k:
        v = v[k:len(v) - k]
    return float(statistics.median(v))


# --------------------------------------------------------------------------- Maengel-Scanner
# Prueft Titel, Zustand und die KOMPLETTE Beschreibung satzweise auf Maengel, Sperren, fehlende Teile,
# schlechte Gesundheitswerte und Betrugsmuster.
#   HARD  = Anzeige wird ausgeschlossen
#   SOFT  = unklarer Hinweis: nur mit Warnung in der Mail und Punktabzug
#   CLEAN = nichts gefunden
# Gesucht wird auf "normalisiertem" Text (klein, ae/oe/ue/ss statt Umlaute), darum stehen die Muster ohne Umlaute.

HARD, SOFT, CLEAN = "HARD", "SOFT", "CLEAN"
SHORT_DESC_MIN = 40        # kuerzere Beschreibung bei teurer Ware ist verdaechtig
SHORT_DESC_PRICE = 100


def norm(s: str | None) -> str:
    """Kleinschreibung, Umlaute ausgeschrieben ('für' = 'fuer'), damit Schreibvarianten gleich behandelt werden."""
    s = (s or "").lower().replace(" ", " ")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


_DET = (r"(?:(?:das|den|die|dem|der|ein|eine|einen|original|originales|originalen|passendes|passenden|"
        r"zugehoeriges|dazugehoeriges|externes|externen)\s+)?")
_STROM = r"(?:netzteil|ladegeraet|ladekabel|akku|batterie)"
_SPEICHER = r"(?:ssd|festplatte|hdd|ram|arbeitsspeicher|speicher|laufwerk|trays?|einschuebe|festplattenrahmen)"
_TEILE = (r"(?:display|bildschirm|luefter|kuehler|tastatur|gehaeuse|cpu|prozessor|mainboard|motherboard|"
          r"rahmen|schrauben|backplate|rueckwand)")
_BAUTEIL = (r"(?:tastatur|tasten?|touchpad|trackpoint|trackpad|ladebuchse|netzbuchse|ladeanschluss|"
            r"usb(?:-c)?(?:\s*(?:port|anschluss|buchse))?|hdmi(?:\s*(?:port|anschluss|buchse))?|displayport|"
            r"kamera|webcam|mikrofon|lautsprecher|boxen|wlan|wifi|bluetooth|scharnier\w*|display|bildschirm|"
            r"touchscreen|touchbar|fingerabdrucksensor|fingerprint|kartenleser|luefter|gehaeuse|monitor|"
            r"anschluesse?|buchse|ports?|schnittstelle\w*|laufwerk)")
# Ausstattungsstufen ("159 € ohne RAM, 195 € mit 8 GB") sind keine fehlenden Teile, sondern Varianten
_VARIANTE = (r"optional|konfigur|waehlbar|aufpreis|\+\s*\d+\s*(?:€|eur)|\bmit\s+\d+\s*gb|\bje\s+nach|\bstufe|"
             r"\bvariante|\bwahlweise|\bab\s+\d+\s*(?:€|eur)")


def _fehlt(teile: str) -> str:
    return (r"\bohne\s+" + _DET + teile + r"\b"
            r"|\bkein(?:e|en|es)?\s+(?:original\w*\s+)?" + teile + r"\b"
            r"|\b" + teile + r"\s+(?:\w+\s+){0,2}(?:nicht\s+(?:dabei|enthalten|vorhanden|inklusive|inkl|"
            r"im\s+lieferumfang|im\s+preis|mit\s+dabei|mitgeliefert)|fehlt|fehlen|extra|gegen\s+aufpreis|"
            r"zusaetzlich|separat)\b")


# (Name, Muster, Stufe, selbstverneinend, abmildern_wenn)
#  selbstverneinend = True: das Wort "nicht/kein" gehoert zum Mangel ("funktioniert nicht"), Verneinungen davor zaehlen nicht
#  abmildern_wenn = Muster im selben Satz, dann nur SOFT statt HARD
RULES = [
    ("defekt", r"\b(?:teil)?defekt\w*|\bkaputt\w*|\bfunktionsunfaehig\w*|\bfunktionsuntuechtig\w*|\bunbrauchbar\w*|"
               r"\bhinueber\b|\bausgefallen\b|\bausfall\b|\bmangelhaft\w*", HARD, False, None),
    ("bastler", r"\bbastler\w*|\bbastelobjekt\w*|\bbastelprojekt\w*|\bersatzteil\w*|\bteilespender\w*|"
                r"\bausschlacht\w*|\bschrott\w*|\bwracks?\b|\bzum\s+(?:basteln|ausschlachten|reparieren)\b|"
                r"\bfuer\s+(?:die\s+)?(?:reparatur|bastler|teile)\b", HARD, False, None),
    ("reparatur_noetig", r"\breparaturbeduerftig\w*|\bzu\s+reparieren\b|\bmuss\s+(?:\w+\s+){0,3}repariert\b|"
                         r"\breparieren\s+lassen\b|\bbedarf\s+(?:einer\s+)?reparatur\b", HARD, True, None),
    ("repariert", r"\brepariert\w*|\breparatur\w*|\bnachgeloetet\w*|\bgeloetet\w*|\bnachgeklebt\w*|\bgeklebt\w*|"
                  r"\bneu\s+verklebt\w*", SOFT, False, None),
    ("funktioniert_nicht",
     r"\bfunktioniert\s+(?:\w+\s+){0,2}(?:nicht|nie|kaum|teilweise|zeitweise|manchmal|nur\s+noch)\b"
     r"|\b(?:geht|ging|gehen)\s+(?:\w+\s+){0,2}(?:nicht|nie)\b"
     r"|\b(?:laeuft|lief)\s+(?:\w+\s+){0,2}(?:nicht|nie)\b(?!\s+(?:zu\s+)?(?:heiss|warm|laut|ruckel\w*|haeng\w*|ueber))"
     r"|\bstartet\s+(?:\w+\s+){0,2}nicht\b|\bbootet\s+(?:\w+\s+){0,2}nicht\b"
     r"|\bfaehrt\s+(?:\w+\s+)?nicht\s+hoch\b|\bschaltet\s+(?:sich\s+)?(?:\w+\s+)?nicht\s+(?:ein|an|mehr)\b"
     r"|\blaedt\s+(?:\w+\s+){0,2}nicht\b|\berkennt\s+(?:\w+\s+){0,2}nicht\b|\breagiert\s+(?:\w+\s+){0,2}nicht\b"
     r"|\bnicht\s+(?:mehr\s+)?(?:funktionsfaehig|funktionstuechtig|nutzbar|verwendbar|einsatzfaehig|"
     r"betriebsbereit|zu\s+gebrauchen)\b|\bnicht\s+mehr\s+an\b", HARD, True, None),
    ("kein_bild", r"\bkein\s+(?:bild|signal|ton|sound)\b|\bschwarzer\s+bildschirm\b|\bblack\s*screen\b|"
                  r"\bbildschirm\s+bleibt\s+schwarz\b", HARD, True, None),
    ("ungetestet", r"\bungetestet\w*|\bnicht\s+(?:\w+\s+)?(?:getestet|testbar|ausprobiert|geprueft|ueberprueft)\b|"
                   r"\bkonnte\s+(?:\w+\s+){0,2}nicht\s+(?:\w+\s+)?(?:getestet|geprueft)\b|\bohne\s+(?:\w+\s+)?test\b|"
                   r"\b(?:konnte|kann|habe|haben)\s+(?:\w+\s+){0,3}nicht\s+(?:testen|pruefen|ueberpruefen|ausprobieren)\b",
     HARD, True, None),
    ("schaden", r"\bgebrochen\w*|\bbruchschaden\w*|\bglasbruch\w*|\bgerissen\w*|\bhaarriss\w*|\brisse?\b|"
                r"\bgesprungen\w*|\bsprung\b|\bsplitter\w*|\babgebrochen\w*|\bausgebrochen\w*|\babgerissen\w*|"
                r"\bverbogen\w*|\bschaden\b|\bschaeden\b|\bbeschaedigt\w*|\bbeschaedigung\w*",
     HARD, False, r"verpackung|karton|\bovp\b|schachtel|umverpackung|huelle|\bbox\b"),
    ("fluessigkeit", r"\bwasserschaden\w*|\bfluessigkeitsschaden\w*|\bfeuchtigkeitsschaden\w*|"
                     r"\bwasser\s+(?:abbekommen|eingedrungen)|\bverschuettet\w*|\bkorrosion\w*|\bkorrodiert\w*|"
                     r"\boxidier\w*|\boxidation\w*|\bsturzschaden\w*|\bfallschaden\w*|\bheruntergefallen\w*|"
                     r"\bruntergefallen\w*|\bhingefallen\w*|\bgestuerzt\w*", HARD, False, None),
    ("bildfehler", r"\bbildfehler\w*|\bpixelfehler\w*|\btote[nr]?\s+pixel\w*|\bhot\s*pixel\w*|\bartefakt\w*|"
                   r"\bgrafikfehler\w*|\bbildstoerung\w*|\bbildaussetzer\w*|\bflackert\w*|\bflackern\w*|"
                   r"\bflimmer\w*|\bdisplayschaden\w*|\bdisplaybruch\w*|\bbildschirmschaden\w*|"
                   r"\bdisplayfehler\w*|\beinbrenn\w*|\bburn[- ]?in\b|\bnachleucht\w*|"
                   r"\b(?:streifen|linien)\s+im\s+(?:display|bild|bildschirm)\b|"
                   r"\bvertikale[rn]?\s+(?:linie|streifen)\w*|\bhorizontale[rn]?\s+(?:linie|streifen)\w*|"
                   r"\bschatten\s+im\s+(?:display|bild)\b|\b(?:dunkle|helle)\s+(?:stelle|flecken)\w*",
     HARD, False, None),
    ("optik_geraeusch", r"\bflecken?\b|\bfleck\b|\bbacklight\s*bleeding\b|\blichthof\w*|\bklebereste?\b|"
                        r"\baufkleberreste?\b|\bverfaerbung\w*|\bvergilbt\w*|\bangeschlagen\w*|\bverbeult\w*|"
                        r"\babgeplatzt\w*|\bspulenfiepen\w*|\bfiepen\w*|\bbrummen\w*|\bbrummt\b|\bsurrt\b|"
                        r"\bgeraeusch\w*", SOFT, False, None),
    ("akku_defekt", r"\bakku\s+(?:\w+\s+){0,2}(?:defekt|tot|leer|schwach|schlecht|hinueber|verbraucht|kaputt|"
                    r"ausgeleiert|aufgebl\w+|tiefentladen)\b|\baufgebl\w+|\bblaeht\s+sich\b|"
                    r"\bakku\s+(?:muss|sollte)\s+(?:\w+\s+){0,2}(?:getauscht|ersetzt|erneuert)\b|"
                    r"\bakku\s+(?:\w+\s+)?(?:tauschen|wechseln|erneuern)\b", HARD, False, None),
    ("akku_haelt_nicht", r"\bakku\s+(?:\w+\s+){0,3}haelt\s+(?:\w+\s+){0,2}(?:nicht|kaum|nur)\b|"
                         r"\bakku\s+(?:\w+\s+){0,3}(?:nur\s+noch|noch\s+nur)\b|"
                         r"\bakku\s+(?:\w+\s+){0,3}laedt\s+(?:\w+\s+)?nicht\b", HARD, True, None),
    ("bauteil_defekt", r"\b" + _BAUTEIL + r"\s+(?:\w+\s+){0,2}(?:defekt\w*|kaputt|lose|locker|wackelt|wackelig|"
                       r"fehlt|fehlen|gebrochen|abgebrochen|klemmt|haengt|knarzt|knackt|ausgeleiert|schief|"
                       r"verbogen|verzogen|spinnt|zickt|ruckelt)\b", HARD, False, None),
    ("bauteil_geht_nicht", r"\b" + _BAUTEIL + r"\s+(?:\w+\s+){0,2}(?:funktioniert\s+(?:\w+\s+)?nicht|geht\s+nicht|"
                           r"gehen\s+nicht|reagiert\s+nicht|ohne\s+funktion|nicht\s+(?:mehr\s+)?"
                           r"(?:funktion\w*|nutzbar|verfuegbar|vorhanden|erkannt))\b", HARD, True, None),
    ("absturz", r"\bwackelkontakt\w*|\bkontaktproblem\w*|\bkontaktfehler\w*|\bfehlfunktion\w*|\btotalausfall\w*|"
                r"\bbootloop\w*|\bblue\s*screen\w*|\bkernel\s+panic\w*|\babsturz\w*|\babstuerz\w*|"
                r"\bstuerzt\s+(?:\w+\s+)?ab\b|\bhaengt\s+sich\s+auf\b|\bfriert\s+(?:\w+\s+)?ein\b|\beinfrier\w*|"
                r"\baussetzer\w*|\bausfaelle\b|\bueberhitz\w*|\bthermal\s+throttl\w*|\bthrottelt\w*|"
                r"\bwird\s+(?:sehr\s+|extrem\s+|viel\s+zu\s+|zu\s+)?heiss\b|\bheisslaeufer\w*", HARD, False, None),
    ("schaltet_ab", r"\bschaltet\s+sich\s+(?:\w+\s+){0,3}(?:zufaellig|ploetzlich|unerwartet|staendig|"
                    r"selbststaendig|von\s+selbst)\b|\bschaltet\s+sich\s+(?:immer\s+|dann\s+)?(?:ab|aus)\b",
     HARD, False, None),
    ("luefter_geraeusch", r"\bluefter\s+(?:\w+\s+){0,3}(?:laut|schleift|rattert|klappert|defekt|faellt\s+aus|"
                          r"brummt|kreischt)\b|\b(?:rattert|klappert|schleift|klackert|klackernd\w*|kreischt|"
                          r"schleifgeraeusch\w*|klickgeraeusch\w*|knackgeraeusch\w*|klickt|kopfcrash|headcrash)\b",
     HARD, False, None),
    ("smart", r"\breallocated\w*|\breallokiert\w*|\bneu\s+zugewiesen\w*|\bpending\s+sector\w*|"
              r"\bausstehende\s+sektor\w*|\bbad\s+(?:block|sector)\w*|\bdefekte\s+sektor\w*|"
              r"\bdefekte\s+bloecke\w*|\bsmart[- ]?(?:warnung|fehler|caution|warning|bad|problem)\w*|"
              r"\blesefehler\w*|\bschreibfehler\w*|\bcrc[- ]?fehler\w*|\bcaution\b|\bunhealthy\b",
     HARD, False, None),
    ("sperre", r"\b(?:bios|uefi|supervisor|power[- ]?on|setup|festplatten|hdd|ssd|admin\w*|zugangs)[- ]?"
               r"(?:passwort|kennwort|password|pw|sperre|lock)\b|\bpasswort\s+(?:\w+\s+)?(?:vergessen|unbekannt|"
               r"gesperrt)\b|\bicloud[- ]?(?:sperre|lock|gesperrt|aktiv)\w*|\baktivierungssperre\w*|"
               r"\bactivation[- ]?lock\b|\bapple[- ]?id\s+(?:\w+\s+)?(?:gesperrt|gelockt|aktiv)\b|\bmdm\b|"
               r"\bfirmensperre\w*|\bfirmenprofil\w*|\bintune\b|\bcomputrace\b|"
               r"\babsolute\s+(?:secure|software|persistence)\w*|\bgesperrt\w*|\bsperre\b|\bsim[- ]?lock\w*|"
               r"\bcarrier[- ]?lock\w*|\bblacklist\w*|\bals\s+gestohlen\w*|\bgestohlen\w*|"
               r"\bpasswortgeschuetzt\w*|\bentsperr\w*\s+(?:noetig|erforderlich|fehlt)\b", HARD, False, None),
    ("unvollstaendig_strom", _fehlt(_STROM), HARD, True, _VARIANTE),
    ("unvollstaendig_speicher", _fehlt(_SPEICHER), HARD, True, _VARIANTE),
    ("unvollstaendig_teile", _fehlt(_TEILE), HARD, True, _VARIANTE),
    ("nur_teil", r"\bnur\s+(?:das\s+|der\s+|die\s+|ein\s+|eine\s+)?(?:gehaeuse|karton|verpackung|ovp|huelle|"
                 r"kuehler|wasserblock|backplate|platine|blende|abdeckung|rahmen|kabel|zubehoer|halterung|case|"
                 r"box)\b|\bleergehaeuse\w*|\battrappe\w*|\bdummy\b|\bmainboard\s+only\b|\bnur\s+mainboard\b|"
                 r"\bleerkarton\w*", HARD, True, None),
    ("teilweise", r"\bteilweise\s+(?:\w+\s+)?(?:defekt|funktion\w*|kaputt|ausgefallen)\b|"
                  r"\beingeschraenkt\w*\s+(?:funktion\w*|nutzbar\w*|einsatz\w*)|"
                  r"\bzeitweise\s+(?:\w+\s+)?(?:ausfall|aussetzer|probleme)\b", HARD, False, None),
    ("probleme", r"\bprobleme\b|\bproblem\b|\bfehler\b|\bfehlermeldung\w*|\bfehlercode\w*|\bstoerung\w*|"
                 r"\bmangel\b|\bmaengel\b|\bmaengeln\b", SOFT, False, None),
    ("zustand_unklar", r"\bkeine\s+ahnung\b|\bzustand\s+unbekannt\b|\bunbekannter\s+zustand\b|\bangetestet\b|"
                       r"\bkurz\s+getestet\b|\bnur\s+kurz\s+(?:\w+\s+)?(?:getestet|benutzt|ausprobiert)\b|"
                       r"\blaeuft\s+soweit\b|\bsoweit\s+(?:ich\s+weiss|funktionsfaehig|ok|in\s+ordnung)\b|"
                       r"\bbei\s+mir\s+(?:lief|laeuft|ging)\b|\blief\s+(?:bis|zuletzt|noch)\b|"
                       r"\bwie\s+(?:es\s+)?(?:ist|gesehen)\b|\bhaushaltsaufloesung\w*|\baus\s+(?:dem\s+)?nachlass\b|"
                       r"\bgeerbt\w*|\binsolvenz\w*|\bkonkurs\w*|\bgebraucht\s+gekauft\b|\bwundertuete\b|"
                       r"\bretoure\w*|\bgarantie\s+abgelaufen\b", SOFT, True, None),
]

# Betrugsmuster: Zahlung ausserhalb des Kaeuferschutzes und Kontakt ausserhalb von kleinanzeigen
SCAM_RULES = [
    ("zahlung_ohne_schutz",
     r"\bpaypal\s*(?:-\s*)?(?:freunde|friends|f\s*&\s*f|ff|familie)\w*|\bfreunde\s*(?:und|&|/)\s*familie\b|"
     r"\bfamilie\s*(?:und|&|/)\s*freunde\b|\bfriends\s*(?:and|&)\s*family\b|\bf\s*&\s*f\b|\bwestern\s+union\b|"
     r"\bmoneygram\b|\bpaysafe\w*|\bamazon[- ]?gutschein\w*|\bgutscheincode\w*|\bbitcoin\w*|\bkrypto\w*|"
     r"\bvorkasse\b|\bueberweisung\s+(?:vorab|vorher|zuerst|im\s+voraus)\b|\bnur\s+ueberweisung\b", HARD),
    ("kontakt_ausserhalb",
     r"\b(?:whatsapp|whats\s*app|telegram|threema|viber|signal[- ]?app)\b|[\w.+-]+@[\w-]+\.[a-z]{2,}|"
     r"(?:\+|\b00)\s*49[\s\d/-]{8,}|\b0\s*1[5-7]\d[\s\d/-]{7,}", HARD),
    ("fremder_link", r"https?://|\bwww\.[a-z0-9-]+\.", SOFT),
]
_PROTECT = re.compile(r"sicher\s+bezahlen|kaeuferschutz|waren\s*(?:und|&)\s*dienstleistung|direkt\s+kaufen|"
                      r"paypal\s+(?:waren|kaeuferschutz)")

_RULES_C = [(n, re.compile(rx), lv, sn, re.compile(si) if si else None) for n, rx, lv, sn, si in RULES]
_SCAM_C = [(n, re.compile(rx), lv) for n, rx, lv in SCAM_RULES]

_SPLIT = re.compile(r"[\r\n]+|(?<=[.!?])\s+|\s[•·|*]\s|\s[–—]\s|\s-\s")
_NEG_WORDS = {"kein", "keine", "keinen", "keinem", "keiner", "keines", "keinerlei", "nicht", "nie", "niemals",
              "nichts", "ohne", "weder", "frei", "null", "0", "nein"}
_CLAUSE_CUT = re.compile(r"[,;:()!?]|\s(?:aber|sondern|jedoch|allerdings|und|oder|sowie)\s")
_COND_BEFORE = re.compile(
    r"(?:\b(?:falls|wenn|sofern|ob)\s+(?:\w+\s+){0,3}|\bsollte\w*\s+(?:\w+\s+){0,3}|"
    r"\bbei\s+(?:(?:einem|etwaigem|eventuellem|moeglichem)\s+)?|\bim\s+fall(?:e)?\s+(?:eines?\s+)?|"
    r"\b(?:garantie|gewaehrleistung|haftung|rueckgabe|ruecknahme|erstattung|ersatz|umtausch|rueckerstattung)\s+"
    r"(?:\w+\s+){0,2}(?:auf|bei|fuer|von)\s+(?:\w+\s+)?)$")
# "Wasserschaden: nein", "Defekte: keine", "Reallocated: 0", "Wasserschaden ausgeschlossen"
_NEG_AFTER = re.compile(
    r"^\s*[:=-]\s*(?:keine?n?\b|nein\b|0\b|null\b|nicht\s+vorhanden|nix\b|nichts\b|none\b)"
    r"|^\s+(?:keine?n?\b|nein\b|ausgeschlossen\b|nicht\s+vorhanden|nix\b|nichts\b)")
# "defekte Sektoren: 0", "Reallokierte/defekte Sektoren: 0", "pending sector count: 0"
_ZERO_AFTER = re.compile(
    r"^[\s/,&-]*(?:\w{0,12}[\s/,&-]+)?(?:sektor|sector|pixel|bloc?k|bloecke|zellen|count|zaehler|events?)\w*"
    r"\s*[:=]?\s*(?:0\b|null\b|keine?\b|nein\b|none\b|no\b)")

_HEALTH_DISK = [re.compile(r"(?:ssd|hdd|nvme|festplatte|laufwerk|datentraeger)\w*[-\s]*(?:gesundheit|health|zustand|"
                           r"lebensdauer|restlebensdauer|verschleiss)\D{0,14}?(\d{1,3})\s*%"),
                re.compile(r"(?:gesundheit|health)\s*(?:der\s+)?(?:ssd|hdd|nvme|festplatte)\D{0,14}?(\d{1,3})\s*%")]
_HEALTH_AKKU = re.compile(r"akku\w*[-\s]*(?:gesundheit|health|zustand|kapazitaet|restkapazitaet|verschleiss)?"
                          r"\D{0,14}?(\d{2,3})\s*%")


def _negated(before: str, matched: str, after: str) -> bool:
    """Steht vor/in/nach dem Treffer eine Verneinung ("kein Defekt", "ohne Wasserschaden", "Defekte: 0")?"""
    cut = 0
    for m in _CLAUSE_CUT.finditer(before):
        cut = m.end()
    words = re.findall(r"\w+", before[cut:])[-3:]
    if any(w in _NEG_WORDS for w in words):
        return True
    if any(w in _NEG_WORDS for w in re.findall(r"\w+", matched)[:-1]):  # "akku nicht defekt"
        return True
    return bool(_NEG_AFTER.match(after) or _ZERO_AFTER.match(after))


def _sentences(text: str | None) -> list[str]:
    return [s.strip() for s in _SPLIT.split(text or "") if s and s.strip()]


def _health_hits(whole: str) -> list[tuple]:
    out = []
    for rx in _HEALTH_DISK:
        for m in rx.finditer(whole):
            val = int(m.group(1))
            if "verschleiss" in m.group(0):
                val = 100 - val
            if val < 80:
                out.append((HARD, "ssd_gesundheit", m.group(0), f"SSD/HDD-Gesundheit {val} %", "Beschreibung"))
            elif val < 90:
                out.append((SOFT, "ssd_gesundheit", m.group(0), f"SSD/HDD-Gesundheit {val} %", "Beschreibung"))
    for m in _HEALTH_AKKU.finditer(whole):
        ctx = whole[max(0, m.start() - 20): m.end() + 25]
        if re.search(r"geladen|ladung|ladestand|laden\b", ctx):
            continue
        val = int(m.group(1))
        if "verschleiss" in m.group(0):
            val = 100 - val
        if val < 70:
            out.append((HARD, "akku_gesundheit", m.group(0), f"Akku nur {val} %", "Beschreibung"))
        elif val < 80:
            out.append((SOFT, "akku_gesundheit", m.group(0), f"Akku nur {val} %", "Beschreibung"))
    return out


@functools.lru_cache(maxsize=30000)
def _scan_cached(title: str, desc: str, ignore: tuple) -> tuple:
    hits: list[tuple] = []
    whole = norm((title or "") + " \n " + (desc or ""))
    protected = bool(_PROTECT.search(whole))
    for src, text in (("Titel", title), ("Beschreibung", desc)):
        for sent in _sentences(text):
            n = norm(sent)
            for name, rx, lvl, selfneg, soft_if in _RULES_C:
                if name in ignore:
                    continue
                for m in rx.finditer(n):
                    before, after = n[:m.start()], n[m.end(): m.end() + 40]
                    if _COND_BEFORE.search(before):
                        continue
                    if not selfneg and _negated(before, m.group(0), after):
                        continue
                    lv = SOFT if (soft_if and soft_if.search(n)) else lvl
                    hits.append((lv, name, m.group(0), sent[:160], src))
                    break  # ein Treffer je Regel und Satz genuegt
            for name, rx, lvl in _SCAM_C:
                for m in rx.finditer(n):
                    before, after = n[:m.start()], n[m.end(): m.end() + 40]
                    if _COND_BEFORE.search(before) or _negated(before, m.group(0), after):
                        continue
                    lv = SOFT if (lvl == HARD and protected) else lvl
                    hits.append((lv, name, m.group(0), sent[:160], src))
                    break
    hits += _health_hits(whole)
    seen, uniq = set(), []
    for h in hits:  # Titel wird oft in der Beschreibung wiederholt -> nicht doppelt zaehlen
        key = (h[0], h[1], h[2])
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return tuple(uniq)


def scan_defects(title: str = "", desc: str = "", cond: str = "", ignore=(), price: float | None = None) -> dict:
    """Prueft Titel + Beschreibung (+ Zustandsfeld). Gibt {"level": HARD|SOFT|CLEAN, "hits": [...]} zurueck.
    ignore = Regelnamen, die fuer diese Produktgruppe normal sind (z. B. NAS ohne Festplatte)."""
    hits = [dict(level=h[0], rule=h[1], word=h[2], snippet=h[3], source=h[4])
            for h in _scan_cached(title or "", desc or "", tuple(sorted(ignore or ())))]
    if norm(cond).strip() == "defekt":
        hits.append(dict(level=HARD, rule="zustand_defekt", word="Defekt", snippet="Zustand laut Anzeige: Defekt",
                         source="Zustand"))
    d_text = (desc or "").strip()
    if price and price >= SHORT_DESC_PRICE and len(d_text) < SHORT_DESC_MIN:
        hits.append(dict(level=SOFT, rule="beschreibung_kurz", word="", source="Beschreibung",
                         snippet=f"Beschreibung nur {len(d_text)} Zeichen bei {price:.0f} € Preis"))
    elif d_text and norm(d_text).strip(" .!") == norm(title).strip(" .!"):
        hits.append(dict(level=SOFT, rule="beschreibung_wie_titel", word="", source="Beschreibung",
                         snippet="Beschreibung wiederholt nur den Titel (keine Angaben zu Zustand/Mängeln)"))
    level = HARD if any(h["level"] == HARD for h in hits) else (SOFT if hits else CLEAN)
    return {"level": level, "hits": hits}


def describe_hit(h: dict) -> str:
    return f"{h['rule']} („{h['snippet'].strip()}“)"


def mentions_defect(text: str) -> bool:
    """Kurzform: enthaelt der Text einen harten Mangelhinweis?"""
    return scan_defects("", text)["level"] == HARD


# --------------------------------------------------------------------------- Marken und Qualitaetsstufen
# A = etablierte Markenhersteller, B = akzeptable Mittelklasse/OEM, C = No-Name/China-Billigware.
# Die Listen stehen in marken.toml (dort bearbeiten). Marktpreise werden pro Stufe getrennt berechnet.

_MARKEN: dict | None = None


def _alt(items) -> re.Pattern | None:
    if not items:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(norm(x) for x in items) + r")(?![a-z0-9])")


def _alt_free(items) -> re.Pattern | None:
    """Modellreihen (Muster duerfen Ziffern direkt anhaengen, z. B. 'rtx\\s?\\d{4}')."""
    if not items:
        return None
    return re.compile(r"(?<![a-z])(?:" + "|".join(norm(x) for x in items) + r")")


def get_marken() -> dict:
    global _MARKEN
    if _MARKEN is None:
        if not os.path.exists(MARKEN_PATH):
            sys.exit("marken.toml fehlt – bitte die Datei ins Repo hochladen (neben deal_watcher.py).")
        with open(MARKEN_PATH, "rb") as f:
            raw = tomllib.load(f)
        groups = {}
        for name, g in raw.get("gruppe", {}).items():
            groups[name] = {
                "A": _alt(g.get("A")), "B": _alt(g.get("B")), "C": _alt(g.get("C")),
                "mA": _alt_free(g.get("modell_A")), "mB": _alt_free(g.get("modell_B")),
                "unbekannt": g.get("unbekannt", "C"), "ignoriere": tuple(g.get("ignoriere", [])),
            }
        floors = [(f["gruppe"], re.compile(norm(f["muster"])), float(f["unter"])) for f in raw.get("preisboden", [])]
        _MARKEN = {"gruppe": groups, "boden": floors}
    return _MARKEN


def detect_brand(gruppe: str | None, title: str, extra: str = "") -> tuple[str | None, str | None]:
    """Gibt (Marke, Stufe) zurueck. Stufe C gewinnt immer (eine Billigmarke im Titel entwertet die Anzeige),
    danach zaehlen Modellreihen (z. B. ThinkPad = A), dann der Markenname, der im Titel zuerst steht."""
    g = get_marken()["gruppe"].get(gruppe) if gruppe else None
    if not g:
        return None, None
    t = norm(title)
    if g["C"]:
        m = g["C"].search(t)
        if m:
            return m.group(0), "C"
    model = None
    for key, tier in (("mA", "A"), ("mB", "B")):
        if g[key]:
            m = g[key].search(t)
            if m:
                model = (m.group(0), tier)
                break
    best = None
    for tier in ("A", "B"):
        if g[tier]:
            m = g[tier].search(t)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.group(0), tier)
    if model:  # Modellreihe legt die Stufe fest; als Name zeigen wir die Marke, falls eine im Titel steht
        return (best[1] if best else model[0]), model[1]
    if best:
        return best[1], best[2]
    if extra:
        b2, t2 = detect_brand(gruppe, extra)
        if b2 != "unbekannt":
            return b2, t2
    return "unbekannt", g["unbekannt"]


def group_ignore(q: dict | None) -> tuple:
    gr = (q or {}).get("gruppe")
    g = get_marken()["gruppe"].get(gr) if gr else None
    return g["ignoriere"] if g else ()


def price_floor_hit(q: dict, title: str, price: float | None) -> float | None:
    """Fake-Grenze: ein Preis unter dem Mindestpreis fuer dieses Modell/diese Groesse ist unrealistisch."""
    gr = q.get("gruppe")
    if not gr or price is None:
        return None
    t = norm(title)
    for g, rx, unter in get_marken()["boden"]:
        if g == gr and rx.search(t):
            return unter if price < unter else None
    return None


def model_conflict(title: str, attrs: dict) -> str | None:
    """Grafikkarten: widerspricht das Modell in den Anzeigen-Attributen dem Titel?"""
    rx = re.compile(r"\b(?:rtx|gtx|rx|arc)\s?-?\s?(\d{3,4})(?:\s?(ti|super|xtx|xt))?")
    t = [(m.group(1), m.group(2) or "") for m in rx.finditer(norm(title))]
    a_text = norm(" ".join(str(v) for k, v in attrs.items() if k.lower() in ("modell", "grafikkarte", "gpu", "typ", "art")))
    a = [(m.group(1), m.group(2) or "") for m in rx.finditer(a_text)]
    if not t or not a:
        return None
    if {x[0] for x in t}.isdisjoint({x[0] for x in a}):
        return f"Modell laut Anzeige ({a[0][0]}) widerspricht dem Titel ({t[0][0]})"
    if {x for x in t if x[1]} != {x for x in a if x[1]} and {x[0] for x in t} == {x[0] for x in a}:
        return "Variante (Ti/Super/XT) laut Anzeige widerspricht dem Titel"
    return None


# Neueste Anzeigen zuerst: Seite 1 enthaelt so immer die frischesten Angebote.
SORT = "?sortingField=SORTING_DATE"


def search_url(term: str, category: str | None, page: int = 1) -> str:
    kw = re.sub(r"[^a-z0-9äöüß.]+", "-", term.lower()).strip("-")
    pg = f"seite:{page}/" if page > 1 else ""
    if category:
        slug, cid = CATEGORIES[category]
        return f"{BASE}/s-{slug}/{pg}{kw}/k0c{cid}{SORT}"
    return f"{BASE}/s-{pg}{kw}/k0{SORT}"


# --------------------------------------------------------------------------- Abruf

class Fetcher:
    def __init__(self, pause=(1.5, 3.5)):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        })
        self.pause = pause
        self._last = 0.0
        self.count = 0

    def get(self, url: str) -> str | None:
        wait = random.uniform(*self.pause) - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in (1, 2):
            try:
                r = self.s.get(url, timeout=25)
            except requests.RequestException as e:
                self._last = time.time()
                if attempt == 1:
                    time.sleep(5)
                    continue
                print(f"  ! Netzwerkfehler: {e}")
                return None
            self._last = time.time()
            self.count += 1
            if r.status_code in (403, 429):
                raise Blocked(f"HTTP {r.status_code}")
            if r.status_code == 404:
                return None
            if r.status_code >= 500 and attempt == 1:
                time.sleep(5)
                continue
            if r.status_code != 200:
                print(f"  ! HTTP {r.status_code} bei {url}")
                return None
            # kleinanzeigen schickt UTF-8, oft ohne Zeichensatz im Header -> requests riete sonst Latin-1
            # (dann wird aus "€" Zeichensalat und kein Preis wird erkannt)
            html = r.content.decode("utf-8", errors="replace")
            if re.search(r"vorübergehend gesperrt|IP-Bereich", html, re.I):
                raise Blocked("IP-Bereich vorübergehend gesperrt")
            if re.search(r"captcha|access denied|zugriff verweigert", html, re.I) \
                    and "aditem" not in html and "viewad" not in html:
                raise Blocked("Captcha/Bot-Schutz")
            return html
        return None


# --------------------------------------------------------------------------- Parser

def parse_search(html: str) -> list[dict]:
    """Liest die Trefferliste einer kleinanzeigen-Suchseite."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for art in soup.select("article[data-adid]"):
        adid = art.get("data-adid")
        link = art.select_one("a[href*='/s-anzeige/']")
        href = art.get("data-href") or (link.get("href") if link else None)
        title = ""
        # Titel: erstes Element mit echtem Text (der Bild-Link enthaelt oft nur die Fotoanzahl wie "6")
        for el in art.select(".text-module-begin a, h2 a, h2, h3 a, h3, a.ellipsis, [class*='title'], a[href*='/s-anzeige/']"):
            txt = el.get_text(" ", strip=True) or (el.get("title") or "").strip()
            if len(txt) >= 5 and not re.fullmatch(r"[\d\s]+(Verkäufergarantie|Bilder?)?", txt):
                title = txt
                break
        if not title:
            img = art.select_one("img[alt]")
            title = (img.get("alt") or "").strip() if img else ""
        if not adid or not href or not title:
            continue
        price_el = art.select_one("[class*='price-shipping--price'], .aditem-main--middle--price")
        price_text = ""
        if price_el:
            for old in price_el.select("s, del, [class*='old-price']"):
                old.decompose()
            price_text = price_el.get_text(" ", strip=True)
        full = art.get_text(" ", strip=True)
        if not PRICE_RE.search(price_text):  # Preisfeld nicht gefunden -> im Anzeigentext suchen
            m_pr = re.search(r"\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?\s*€(?:\s*VB)?", full)
            if m_pr:
                price_text = m_pr.group(0)
        loc = art.select_one(".aditem-main--top--left")
        when = art.select_one(".aditem-main--top--right")
        items.append({
            "id": adid,
            "url": href if href.startswith("http") else BASE + href,
            "title": title,
            "price": parse_price(price_text),
            "vb": "VB" in price_text,
            "ship": bool(re.search(r"Versand möglich|Direkt kaufen", full)),
            "direct": "Direkt kaufen" in full,
            "gesuch": bool(re.search(r"^\s*(Gesuch|Suche)\b", title, re.I)) or bool(art.select_one("[class*='gesuch'], [class*='wanted']")),
            "ort": (loc.get_text(" ", strip=True) if loc else "") or ((re.search(r"\b\d{5}\s+[A-ZÄÖÜ][a-zäöüß.\-]+(?:\s+-\s+[A-ZÄÖÜ][\wäöüß.\-]+)?", full) or [""])[0].strip()),
            "datum": when.get_text(" ", strip=True) if when else "",
        })
    if not items:  # Notfall-Parser, falls sich das Seitenlayout aendert
        chunks = re.split(r'(?=<article[^>]*data-adid=")', html)
        for ch in chunks[1:]:
            m_id = re.search(r'data-adid="(\d+)"', ch)
            m_link = re.search(r'href="(/s-anzeige/[^"]+)"[^>]*>\s*([^<]{3,})<', ch)
            if not (m_id and m_link):
                continue
            text = re.sub(r"<[^>]+>", " ", ch)
            items.append({
                "id": m_id.group(1), "url": BASE + m_link.group(1),
                "title": m_link.group(2).strip(), "price": parse_price(text),
                "vb": " VB" in text, "ship": bool(re.search(r"Versand möglich|Direkt kaufen", text)),
                "direct": "Direkt kaufen" in text, "gesuch": bool(re.search(r"\bGesuch\b", text)),
                "ort": "", "datum": "",
            })
    return items


def parse_attrs(soup) -> dict:
    """Liest die Attributliste der Anzeige (Zustand, Marke, Modell, Art ...) als {Name: Wert}."""
    attrs: dict[str, str] = {}
    for li in soup.select("#viewad-details li, .addetailslist li, .addetailslist--detail"):
        val_el = li.select_one("[class*='detail--value'], span")
        if not val_el:
            continue
        val = val_el.get_text(" ", strip=True)
        label = li.get_text(" ", strip=True).replace(val, "", 1).strip(" :")
        if label and val and label not in attrs:
            attrs[label] = val
    return attrs


def parse_detail(html: str) -> dict:
    """Liest Verkaeufer-, Versand- und Zustandsinfos von einer Anzeigenseite."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    cut = len(text)
    for marker in ("Anzeigen-ID", "Andere Anzeigen des Anbieters", "Das könnte dich auch interessieren"):
        i = text.find(marker)
        if 0 < i < cut:
            cut = i
    main = text[:cut]

    d: dict = {}
    title_el = soup.select_one("#viewad-title")
    d["title"] = title_el.get_text(" ", strip=True) if title_el else ""
    price_el = soup.select_one("#viewad-price")
    d["price"] = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
    m = re.search(r"Aktiv seit\s*(\d{2})\.(\d{2})\.(\d{4})", main)
    d["since"] = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    anchor = re.search(r"Privater Nutzer|Gewerblicher (Nutzer|Anbieter)|Aktiv seit", main)
    seller_box = main[max(0, anchor.start() - 160): anchor.end() + 120] if anchor else ""
    d["badges"] = [b for b in ALL_BADGES if re.search(r"(?<![A-Za-zäöü])" + re.escape(b) + r"(?![a-zäöü])", seller_box)]
    d["commercial"] = bool(re.search(r"Gewerblicher (Nutzer|Anbieter)", main))
    m = re.search(r"Versand ab\s*([\d.,]+)\s*€", main)
    d["ship_cost"] = parse_price(m.group(1) + " €") if m else None
    d["pickup_only"] = bool(re.search(r"nur\s+Abholung", main, re.I)) and d["ship_cost"] is None
    d["direct"] = "Direkt kaufen" in main
    m = re.search(r"Zustand\s+(Neu|Sehr Gut|Gut|In Ordnung|Defekt)", main)
    d["condition"] = m.group(1) if m else ""
    m = re.search(r"(\d+)\s+Anzeigen? online", main)
    d["ads_online"] = int(m.group(1)) if m else None
    crumb = soup.select_one("#vap-brdcrmb, .breadcrump")
    d["category"] = crumb.get_text(" > ", strip=True) if crumb else ""
    # Beschreibung: nur das eigentliche Beschreibungs-Element, NIE ersatzweise die ganze Seite
    desc_el = None
    for sel in ("#viewad-description-text", "p[itemprop='description']", "[itemprop='description']",
                "#viewad-description"):
        desc_el = soup.select_one(sel)
        if desc_el:
            break
    d["desc_found"] = desc_el is not None
    d["desc"] = desc_el.get_text("\n", strip=True) if desc_el else ""
    d["attrs"] = parse_attrs(soup)
    # Gesuch statt Angebot: Markierung auf der Seite oder "Ich suche ..." in Titel/Beschreibung
    d["gesuch"] = bool(re.search(r"\bGesuch\b", main[:2000])) or bool(
        re.search(r"^\s*(ich\s+)?(suche|kaufe|ankauf)\b|\bich\s+suche\b|\bsuche\s+(eine?n?|nach)\b|\bwer\s+verkauft\b",
                  f"{d['title']} . {d['desc'][:400]}", re.I))
    d["unsafe_pay"] = bool(re.search(r"freunde|familie|überweisung|vorkasse|nur\s+bar", d["desc"], re.I)) \
        and not re.search(r"waren\s*(&|und)\s*dienst|käuferschutz|sicher\s+bezahlen", d["desc"], re.I)
    return d


# --------------------------------------------------------------------------- Logik

def why_not(item: dict, q: dict) -> str | None:
    """Grund, warum eine Anzeige nicht zur Suche passt (None = passt)."""
    t = item["title"]
    if item["gesuch"]:
        return "Gesuch"
    if item["price"] is None:
        return "kein Preis"
    if TITLE_EXCLUDE.search(t):
        return "Ausschlusswort"
    sc = scan_defects(t, "", ignore=group_ignore(q))
    if sc["level"] == HARD:
        return "Titel nennt Mangel: " + describe_hit(next(h for h in sc["hits"] if h["level"] == HARD))
    if price_floor_hit(q, t, item["price"]):
        return f"Preis unter Fake-Grenze ({price_floor_hit(q, t, item['price']):.0f} €)"
    if not q.get("mengen_erlaubt", False) and LOT_RE.search(t):
        return "Sammelanzeige"
    if any(not re.search(rx, t, re.I) for rx in q.get("muss", [])):
        return "Pflichtbegriff fehlt"
    if any(re.search(rx, t, re.I) for rx in q.get("nicht", [])):
        return "nicht-Begriff"
    if not q.get("preis_min", 0) <= item["price"] <= q.get("preis_max", 10**6):
        return "ausserhalb Preisrahmen"
    return None


def matches(item: dict, q: dict) -> bool:
    return why_not(item, q) is None


def base_score(discount_pct: float) -> int:
    if discount_pct >= 50:
        return 9
    if discount_pct >= 40:
        return 8
    if discount_pct >= 30:
        return 7
    if discount_pct >= 25:
        return 6
    return 5


def assess(cand: dict, d: dict, cfg: dict, q: dict | None = None) -> tuple[int | None, list[str], str | None]:
    """Gibt (Punkte-Modifikator, Notizen, Ausschlussgrund) zurueck.
    Legt das Pruefergebnis zusaetzlich in d["scan"] (Maengel-Scanner) ab."""
    notes: list[str] = []
    mod = 0
    age = (today() - d["since"]).days if d["since"] else None
    positive = [b for b in d["badges"] if b in POSITIVE_BADGES]
    q = q or {}

    if d.get("gesuch"):
        return None, notes, "ist ein Gesuch (jemand sucht), kein Angebot"
    if d["condition"] == "Defekt":
        return None, notes, "Zustand: defekt"
    if not d.get("desc_found", True):
        return None, notes, "Beschreibung nicht lesbar (Seitenlayout geändert?) – nicht blind melden"
    # Marke/Modell laut Anzeige muss zum Titel passen
    gr = q.get("gruppe")
    attrs = d.get("attrs") or {}
    if gr:
        _, t_title = detect_brand(gr, cand.get("title", ""))
        a_brand = attrs.get("Marke") or ""
        if a_brand:
            ab, at = detect_brand(gr, a_brand)
            if at == "C" and t_title != "C":
                return None, notes, f"Marke laut Anzeige „{a_brand}“ ist Stufe C, Titel behauptet etwas anderes"
        mc = model_conflict(cand.get("title", ""), attrs)
        if mc:
            return None, notes, mc
    sc = scan_defects(d.get("title") or cand.get("title", ""), d["desc"], d["condition"],
                      ignore=group_ignore(q), price=cand.get("price"))
    d["scan"] = sc
    if sc["level"] == HARD:
        hard = [h for h in sc["hits"] if h["level"] == HARD]
        return None, notes, "Mangelhinweis: " + "; ".join(describe_hit(h) for h in hard[:2])
    if sc["level"] == SOFT:
        mod -= int(cfg["soft_abzug"])
        notes.append("Hinweis in der Beschreibung: " + "; ".join(describe_hit(h) for h in sc["hits"][:2]))
    if "Na ja Zufriedenheit" in d["badges"]:
        return None, notes, "schlechte Verkäuferbewertung"
    if d["category"] and not re.search(r"Elektronik|PC|Computer|Notebook|Multimedia", d["category"]):
        return None, notes, f"fachfremde Kategorie ({d['category']}) – Betrugsverdacht"
    if age is not None and age < cfg["sperr_konto_tage"]:
        return None, notes, f"Konto erst {age} Tage alt"
    if cfg["nur_mit_versand"] and d["pickup_only"]:
        return None, notes, "nur Abholung"

    old_enough = age is not None and age >= cfg["serioes_konto_tage"]
    # seriös = etabliertes Konto mit positivem Siegel, oder etablierter gewerblicher Händler
    serioes = old_enough and (bool(positive) or d["commercial"])
    if cand["price"] >= cfg["hochpreis_eur"] and not (serioes or d["direct"] or cand.get("direct")):
        return None, notes, f"ab {cfg['hochpreis_eur']:.0f} €: weder etablierter Verkäufer noch Käuferschutz"

    if age is not None:
        if age < cfg["junges_konto_tage"]:
            mod -= 2
            notes.append(f"junges Konto ({age} Tage)")
        else:
            notes.append(f"aktiv seit {d['since'].strftime('%m/%Y')}")
        if age >= cfg["top_konto_tage"] and positive:
            mod += 1
    else:
        notes.append("Kontoalter unbekannt")
    notes.append("gewerblich" if d["commercial"] else "privat")
    if d["badges"]:
        notes.append(", ".join(d["badges"]))
    else:
        if age is not None and age < 365:
            mod -= 1
        notes.append("keine Bewertungssiegel")
    if d["direct"] or cand.get("direct"):
        notes.append("Direkt kaufen mit Käuferschutz")
    elif d.get("unsafe_pay"):
        notes.append("will Zahlung ohne Käuferschutz (Überweisung/PayPal Freunde) – auf „Sicher bezahlen“ bestehen")
    return mod, notes, None


def comparable_prices(it: dict, pool: dict, q: dict, cfg: dict) -> tuple[list[float], float, str | None, str | None, str | None]:
    """Vergleichspreise fuer eine Anzeige. Mit Produktgruppe (marken.toml) wird NUR mit Anzeigen derselben
    Qualitaetsstufe verglichen (A mit A, B mit B ...). Gibt (Preise, Faktor, Marke, Stufe, Hinweis oder Fehlergrund) zurueck;
    sind Preise leer, steht im letzten Feld der Grund."""
    gr = q.get("gruppe")
    n_min = int(cfg["min_vergleiche"])
    if not gr:
        others = [v[0] for k, v in pool.items() if k != it["id"]]
        return (others, 1.0, None, None, None) if len(others) >= n_min else ([], 1.0, None, None, "zu wenige Vergleiche")
    brand, tier = detect_brand(gr, it["title"])
    if tier == "C" and not q.get("noname_melden", cfg["noname_melden"]):
        return [], 1.0, brand, tier, f"Marke „{brand}“ = No-Name/Billigware (Stufe C), wird nicht gemeldet"
    tiers = {k: detect_brand(gr, v[3])[1] for k, v in pool.items() if k != it["id"]}
    same = [pool[k][0] for k, t in tiers.items() if t == tier]
    if len(same) >= n_min:
        return same, 1.0, brand, tier, f"verglichen mit {len(same)} Anzeigen der Stufe {tier}"
    if tier == "B" and cfg["b_mit_a_vergleichen"]:
        a_prices = [pool[k][0] for k, t in tiers.items() if t == "A"]
        if len(a_prices) >= n_min:
            ab = float(cfg["b_abschlag_prozent"])
            return a_prices, 1 - ab / 100, brand, tier, (f"Stufe B mit {len(a_prices)} Stufe-A-Anzeigen verglichen, "
                                                          f"{ab:.0f} % Abschlag")
    return [], 1.0, brand, tier, f"Stufe {tier}: nur {len(same)} Vergleichsanzeigen (nötig: {n_min})"


def qualify_why(it: dict, pool: dict, q: dict, cfg: dict) -> tuple[dict | None, str | None]:
    """Prueft eine Anzeige gegen den Marktpreis ihrer Qualitaetsstufe. Gibt (Kandidat, None) oder (None, Grund)."""
    others, factor, brand, tier, note = comparable_prices(it, pool, q, cfg)
    if not others:
        return None, note
    med = trimmed_median(others) * factor
    disc = (1 - it["price"] / med) * 100
    if it["price"] < med * cfg["zu_billig_prozent"] / 100:
        return None, "verdächtig billig (falscher Artikel, Zubehör oder Fake)"
    own_use = bool(q.get("eigenbedarf", False))
    min_disc = float(q.get("min_rabatt_prozent", cfg["min_rabatt_prozent"]))
    min_margin = 0.0 if own_use else float(q.get("min_spielraum_eur", cfg["min_spielraum_eur"]))
    margin = med - it["price"] - cfg["versand_schaetzung_eur"]
    if disc < min_disc or margin < min_margin:
        return None, "zu wenig Abstand zum Marktpreis"
    if cfg["nur_mit_versand"] and not it["ship"]:
        return None, "kein Versand"
    return {**it, "query": q["name"], "kategorie": q.get("kategorie"), "median": med, "n": len(others),
            "discount": disc, "own_use": own_use, "brand": brand, "tier": tier, "tier_note": note}, None


def qualify(it: dict, pool: dict, q: dict, cfg: dict) -> dict | None:
    return qualify_why(it, pool, q, cfg)[0]


PENDING_KEYS = ("id", "url", "title", "vb", "ship", "direct", "gesuch", "ort", "datum", "query", "kategorie")


def run(cfg: dict, queries: list[dict], state: dict, fetcher: Fetcher) -> tuple[list[dict], dict]:
    t0 = today()
    run_no = state.get("laeufe", 0) + 1
    state["laeufe"] = run_no
    pools = state.setdefault("pools", {})
    reported = state.setdefault("gemeldet", {})
    pending = state.setdefault("offen", {})
    by_name = {q["name"]: q for q in queries}
    stats = {"blocked": None, "listings": 0, "requests": 0, "detail_checked": 0, "detail_unreadable": 0}
    prelim: list[dict] = []

    # Kandidaten, die im letzten Lauf wegen des Pruef-Limits liegen geblieben sind
    for pid, p in list(pending.items()):
        del pending[pid]
        q = by_name.get(p.get("query"))
        pool = pools.get(p.get("query"), {})
        if not q or pid in reported or pid not in pool or p.get("bis", "") < t0.isoformat():
            continue
        c = qualify({**{k: p.get(k) for k in PENDING_KEYS}, "price": pool[pid][0]}, pool, q, cfg)
        if c:
            prelim.append(c)

    try:
        for q in queries:
            every = int(q.get("alle", 1))
            if every > 1 and (run_no - 1) % every:
                continue
            pool = pools.setdefault(q["name"], {})
            # alte Pool-Eintraege ohne Titel (aeltere Version) koennen nicht nachgefiltert werden -> neu aufbauen
            if any(len(v) < 4 for v in pool.values()):
                pool.clear()
            # Vergleichspreise, die nach geaenderten Filtern nicht mehr passen, entfernen
            for k in [k for k, v in pool.items()
                      if why_not({"title": v[3], "price": v[0], "gesuch": False}, q) is not None]:
                del pool[k]
            bootstrap = not pool  # neue Suche: erst Preisbasis sammeln, alte Anzeigen nicht als "neu" melden
            pages = 1 if len(pool) >= cfg["min_vergleiche"] else min(int(cfg["startseiten"]), 5)
            seen: dict[str, dict] = {}
            for page in range(1, pages + 1):
                html = fetcher.get(search_url(q["begriff"], q.get("kategorie"), page))
                if not html:
                    break
                page_items = parse_search(html)
                for it in page_items:
                    seen.setdefault(it["id"], it)
                if len(page_items) < 20:  # letzte Seite erreicht (volle Seite = 25 Anzeigen)
                    break
            stats["listings"] += len(seen)
            relevant = [it for it in seen.values() if matches(it, q)]
            fresh = []
            for it in relevant:
                prev = pool.get(it["id"])
                pool[it["id"]] = [it["price"], t0.isoformat(), prev[2] if prev else t0.isoformat(), it["title"]]
                is_new = prev is None
                cheaper = prev is not None and it["price"] < prev[0] - 0.5
                if (is_new or cheaper) and it["id"] not in reported and not bootstrap:
                    fresh.append(it)
            # alte Vergleichspreise aufraeumen
            limit = (t0 - dt.timedelta(days=int(cfg["pool_tage"]))).isoformat()
            for k in [k for k, v in pool.items() if v[1] < limit]:
                del pool[k]
            med_all = trimmed_median([v[0] for v in pool.values()]) if pool else None
            print(f"- {q['name']}: {len(seen)} Anzeigen, {len(relevant)} passend, Pool {len(pool)}"
                  + (f", Marktpreis ~{fmt_eur(med_all)}" if med_all else "") + f", neu/billiger {len(fresh)}" + (" (erster Lauf: nur Preisbasis)" if bootstrap else ""))
            if seen and not relevant:  # Diagnose, falls nichts passt
                reasons: dict[str, int] = {}
                for it in seen.values():
                    r = why_not(it, q) or "?"
                    reasons[r] = reasons.get(r, 0) + 1
                print("    Diagnose: " + ", ".join(f"{k} {v}x" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
                for it in list(seen.values())[:2]:
                    print(f"    Beispiel: '{it['title'][:60]}' | Preis {it['price']} | Gesuch {it['gesuch']}")

            for it in fresh:
                c, why = qualify_why(it, pool, q, cfg)
                if c:
                    prelim.append(c)
                elif why and ("Stufe" in why or "Marke" in why):
                    print(f"  x {it['title'][:60]} – {why}")
    except Blocked as e:
        stats["blocked"] = str(e)
        print(f"!! Zugriff blockiert: {e} – Lauf abgebrochen")

    # doppelte Kandidaten (z.B. offen + erneut billiger) zusammenfassen
    uniq: dict[str, dict] = {}
    for c in prelim:
        if c["id"] not in uniq or c["discount"] > uniq[c["id"]]["discount"]:
            uniq[c["id"]] = c
    prelim = sorted(uniq.values(), key=lambda c: c["discount"], reverse=True)
    cap = int(cfg["max_detailpruefungen"])
    keep_until = (t0 + dt.timedelta(days=3)).isoformat()

    hits: list[dict] = []
    checked = 0
    if not stats["blocked"]:
        for c in prelim[:cap]:
            try:
                html = fetcher.get(c["url"])
            except Blocked as e:
                stats["blocked"] = str(e)
                break
            checked += 1
            if not html:
                continue
            d = parse_detail(html)
            stats["detail_checked"] += 1
            if not d.get("desc_found"):
                stats["detail_unreadable"] += 1
            if d["price"] and abs(d["price"] - c["price"]) > 0.5:
                # Preis auf der Anzeigenseite weicht ab -> mit diesem Preis nochmal pruefen
                q_c = by_name.get(c["query"], {})
                c2 = qualify({**c, "price": d["price"]}, pools.get(c["query"], {}), q_c, cfg) if q_c else None
                if not c2 or not (q_c.get("preis_min", 0) <= d["price"] <= q_c.get("preis_max", 10**6)):
                    print(f"  x {c['title'][:60]} – Preis auf Anzeigenseite {fmt_eur(d['price'])} passt nicht")
                    continue
                c.update(price=d["price"], discount=c2["discount"])
            mod, notes, reason = assess(c, d, cfg, by_name.get(c["query"]))
            if reason:
                print(f"  x {c['title'][:60]} – {reason}")
                continue
            ship = d["ship_cost"] if d["ship_cost"] is not None else cfg["versand_schaetzung_eur"]
            base = base_score(c["discount"])
            score = base + mod
            score = 10 if (c["discount"] >= 50 and mod >= 1) else max(1, min(9, score))
            if score < cfg["min_bewertung"]:
                print(f"  x {c['title'][:60]} – Bewertung {score}/10 zu niedrig")
                continue
            sc = d.get("scan") or {"level": CLEAN, "hits": []}
            snippet = re.sub(r"\s+", " ", d.get("desc", "")).strip()
            c.update(score=score, notes=notes, ship_cost=d["ship_cost"], condition=d["condition"],
                     margin=c["median"] - c["price"] - ship, desc_len=len(d.get("desc", "")),
                     desc_snip=snippet[:300], scan_level=sc["level"],
                     scan_hits=[describe_hit(h) for h in sc["hits"]])
            hits.append(c)
    # nicht geschaffte Kandidaten beim naechsten Lauf pruefen
    for c in prelim[checked:]:
        pending[c["id"]] = {**{k: c.get(k) for k in PENDING_KEYS}, "bis": keep_until}
    hits.sort(key=lambda c: (c["score"], c["discount"]), reverse=True)
    for c in hits[int(cfg["max_treffer_pro_mail"]):]:
        pending[c["id"]] = {**{k: c.get(k) for k in PENDING_KEYS}, "bis": keep_until}
    hits = hits[: int(cfg["max_treffer_pro_mail"])]
    for h in hits:
        reported[h["id"]] = t0.isoformat()
    # gemeldete IDs nach 90 Tagen vergessen
    old = (t0 - dt.timedelta(days=90)).isoformat()
    for k in [k for k, v in reported.items() if v < old]:
        del reported[k]
    stats["requests"] = fetcher.count
    return hits, stats


# --------------------------------------------------------------------------- Ausgabe

TIER_NAME = {"A": "Markenware", "B": "Mittelklasse/OEM", "C": "No-Name/Billigware"}


def compose(hits: list[dict]) -> tuple[str, str]:
    top = hits[0]
    subj = f"Deal: {top['title'][:55]} für {fmt_eur(top['price'])} (-{top['discount']:.0f} %)"
    if len(hits) > 1:
        subj += f" + {len(hits) - 1} weitere"
    lines = [f"{len(hits)} neue{'s' if len(hits) == 1 else ''} Angebot{'e' if len(hits) > 1 else ''} "
             f"deutlich unter Marktpreis:", ""]
    for i, h in enumerate(hits, 1):
        ship = f" + Versand {fmt_eur(h['ship_cost'])}" if h.get("ship_cost") else " (Versand möglich)"
        lines += [
            f"{i}) Bewertung {h['score']}/10 – {h['title']}",
            f"   Preis: {fmt_eur(h['price'])}{' VB' if h['vb'] else ''}{ship}",
            f"   Marktpreis: ~{fmt_eur(h['median'])} (Median aus {h['n']} Anzeigen \"{h['query']}\")"
            f" -> {h['discount']:.0f} % günstiger",
        ]
        if h.get("brand"):
            lines.append(f"   Marke: {h['brand']} (Stufe {h['tier']}: {TIER_NAME.get(h['tier'], '?')}) – {h.get('tier_note') or ''}")
        if h.get("kategorie") in ("notebooks", "pcs"):
            lines.append("   Achtung: Marktpreis mischt Ausstattungen – CPU/RAM/SSD mit ähnlichen Anzeigen vergleichen")
        if h["own_use"]:
            lines.append("   Eigenbedarf (EliteBook-Upgrade) – passt: DDR4 SO-DIMM bzw. M.2 NVMe laut Titel")
        else:
            lines.append(f"   Spielraum bei Weiterverkauf zum Marktpreis: ca. {fmt_eur(max(h['margin'], 0))}")
        lines.append(f"   Verkäufer: {'; '.join(h['notes'])}")
        if h.get("desc_len") is not None:
            if h.get("scan_hits"):
                lines.append(f"   Beschreibung gelesen ({h['desc_len']} Zeichen) – Hinweise: " + "; ".join(h["scan_hits"][:3]))
            else:
                lines.append(f"   Beschreibung gelesen ({h['desc_len']} Zeichen): keine Mängelhinweise gefunden")
            if h.get("desc_snip"):
                lines.append(f"   „{h['desc_snip']}“")
        extra = f" · Zustand: {h['condition']}" if h.get("condition") else ""
        lines.append(f"   Ort: {h['ort'] or 'k. A.'}{extra}")
        lines.append(f"   {h['url']}")
        if h["score"] == 10:
            lines.append("   -> sofort kaufen, nicht mehr überlegen")
        lines.append("")
    lines.append("Automatisch geprüft (ohne KI). Vor dem Kauf kurz Fotos und Beschreibung selbst checken.")
    return subj, "\n".join(lines)


def send_mail(subject: str, body: str) -> bool:
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if not (user and pw):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.getenv("MAIL_TO") or user
    msg.set_content(body)
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")), timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.send_message(msg)
    return True


def send_issue(subject: str, body: str) -> bool:
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    owner = os.getenv("GITHUB_REPOSITORY_OWNER", "")
    r = requests.post(f"https://api.github.com/repos/{repo}/issues",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"title": subject, "body": f"@{owner}\n\n{body}"}, timeout=30)
    return r.status_code < 300


def send_push(subject: str, body: str, url: str | None) -> None:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    payload = {"topic": topic, "title": subject, "message": body[:900], "priority": 4}
    if url:
        payload["click"] = url
    try:
        requests.post("https://ntfy.sh/", json=payload, timeout=20)
    except requests.RequestException as e:
        print(f"  ! Push fehlgeschlagen: {e}")


def notify(subject: str, body: str, url: str | None = None) -> None:
    send_push(subject, body, url)
    if send_mail(subject, body):
        print("Mail verschickt.")
    elif send_issue(subject, body):
        print("Keine Mail-Zugangsdaten – als GitHub-Issue gemeldet.")
    else:
        print("Keine Benachrichtigung konfiguriert. Inhalt:\n" + subject + "\n" + body)


def handle_block(state: dict, stats: dict, dry: bool) -> None:
    """Nach einer Sperre pausieren: 6 h, bei erneuter Sperre binnen 24 h doppelt so lang (max. 48 h)."""
    h = state.setdefault("zustand", {})
    now = dt.datetime.now()
    if not stats["blocked"]:
        return
    prev_end = dt.datetime.fromisoformat(h["pause_bis"]) if h.get("pause_bis") else None
    prev_h = int(h.get("pause_h", 0))
    hours = min(prev_h * 2, 48) if prev_end and now - prev_end < dt.timedelta(hours=24) and prev_h else 6
    h["pause_h"] = hours
    h["pause_bis"] = (now + dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    print(f"Pause bis {h['pause_bis']} ({hours} h), damit die Sperre nicht verlängert wird.")
    if hours >= 24 and not dry:
        notify("Deal-Watcher: kleinanzeigen sperrt den Server",
               f"kleinanzeigen hat den Deal-Watcher wiederholt gesperrt ({stats['blocked']}). "
               f"Er pausiert jetzt {hours} Stunden und versucht es danach automatisch erneut.\n\n"
               "Deine Suchaufträge in der kleinanzeigen-App laufen davon unabhängig weiter.\n"
               "Wenn das öfter passiert: Skript auf einem eigenen Gerät laufen lassen (ANLEITUNG.md, 'Plan B').")


def health_check(state: dict, stats: dict, dry: bool) -> None:
    h = state.setdefault("zustand", {})
    h.setdefault("leer_in_folge", 0)
    h.setdefault("letzte_warnung", "")
    empty = not stats["blocked"] and stats["listings"] == 0
    h["leer_in_folge"] = h["leer_in_folge"] + 1 if empty else 0
    now = dt.datetime.now()
    last = dt.datetime.fromisoformat(h["letzte_warnung"]) if h["letzte_warnung"] else None
    if last and now - last < dt.timedelta(hours=24):
        return
    det_bad = stats.get("detail_checked", 0) >= 3 and stats.get("detail_unreadable", 0) == stats["detail_checked"]
    if det_bad and not dry:
        notify("Deal-Watcher: Anzeigenseiten nicht lesbar",
               "Die Beschreibung ließ sich bei mehreren Anzeigen nicht lesen. Vermutlich hat kleinanzeigen das "
               "Seitenlayout der Anzeigen geändert. Der Deal-Watcher meldet deshalb vorsichtshalber nichts, "
               "bis der Parser angepasst ist (Claude fragen und das Protokoll zeigen).")
        h["letzte_warnung"] = now.isoformat(timespec="seconds")
        return
    if h["leer_in_folge"] >= 3 and not dry:
        notify("Deal-Watcher: findet keine Anzeigen mehr",
               "Der Deal-Watcher findet seit mehreren Läufen gar keine Anzeigen mehr. "
               "Vermutlich hat kleinanzeigen das Seitenlayout geändert und der Parser muss angepasst werden "
               "(Claude fragen). Die Suchaufträge in der App laufen davon unabhängig weiter.")
        h["letzte_warnung"] = now.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- Start

def load_config() -> tuple[dict, list[dict]]:
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    cfg = {**DEFAULTS, **raw.get("einstellungen", {})}
    queries = raw.get("suche", [])
    names = [q["name"] for q in queries]
    if len(names) != len(set(names)):
        sys.exit("config.toml: jeder Suchname darf nur einmal vorkommen.")
    return cfg, queries


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    state["letzter_lauf"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def explain(target: str, html_file: str | None = None, gruppe: str | None = None) -> None:
    """Zeigt, wie der Deal-Watcher EINE Anzeige beurteilt (Marke/Stufe, gelesene Beschreibung, Mangelhinweise)."""
    if html_file:
        with open(html_file, encoding="utf-8", errors="replace") as f:
            html = f.read()
    else:
        url = target if target.startswith("http") else f"{BASE}/s-anzeige/x/{target}"
        try:
            html = Fetcher().get(url)
        except Blocked as e:
            sys.exit(f"Zugriff blockiert ({e}) – bitte später nochmal oder --html mit gespeicherter Seite nutzen.")
        if not html:
            sys.exit("Anzeige konnte nicht geladen werden (offline, gelöscht oder Netzwerkfehler).")
    d = parse_detail(html)
    print(f"Titel:        {d['title']}")
    print(f"Preis:        {fmt_eur(d['price']) if d['price'] else 'k. A.'} · Zustand: {d['condition'] or 'k. A.'} · "
          f"Versand: {'nur Abholung' if d['pickup_only'] else ('ab ' + fmt_eur(d['ship_cost']) if d['ship_cost'] else 'möglich/unbekannt')}")
    print(f"Verkäufer:    {'gewerblich' if d['commercial'] else 'privat'}, aktiv seit {d['since'] or 'k. A.'}, "
          f"Siegel: {', '.join(d['badges']) or 'keine'}")
    print(f"Attribute:    {d['attrs'] or 'keine gelesen'}")
    print(f"Beschreibung: {'gefunden' if d['desc_found'] else 'NICHT GEFUNDEN'}, {len(d['desc'])} Zeichen")
    if d["desc"]:
        print("              „" + re.sub(r"\s+", " ", d["desc"])[:300] + "“")
    groups = get_marken()["gruppe"]
    shown = [gruppe] if gruppe else [g for g in groups if detect_brand(g, d["title"], d["attrs"].get("Marke", ""))[0] != "unbekannt"]
    print("\nMarke/Stufe:")
    for g in shown or ["(keine bekannte Marke im Titel)"]:
        if g in groups:
            b, t = detect_brand(g, d["title"], d["attrs"].get("Marke", ""))
            print(f"  Gruppe {g}: {b} -> Stufe {t} ({TIER_NAME.get(t, '?')})")
        else:
            print(f"  {g}")
    sc = scan_defects(d["title"], d["desc"], d["condition"], ignore=group_ignore({"gruppe": gruppe}) if gruppe else (), price=d["price"])
    print(f"\nMängel-Scanner: {sc['level']}")
    for h in sc["hits"]:
        print(f"  [{h['level']}] {h['rule']} in {h['source']}: „{h['snippet']}“")
    if not sc["hits"]:
        print("  nichts gefunden")
    if d["gesuch"]:
        print("\n!! Das ist ein Gesuch (jemand sucht), kein Angebot.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Deal-Watcher für kleinanzeigen.de")
    ap.add_argument("--dry-run", action="store_true", help="keine Mail, state.json nicht speichern")
    ap.add_argument("--test-mail", action="store_true", help="nur Testmail senden")
    ap.add_argument("--explain", metavar="ID_ODER_URL", help="eine einzelne Anzeige erklären (Marke, Mängel-Scanner)")
    ap.add_argument("--html", metavar="DATEI", help="mit --explain: gespeicherte Anzeigenseite statt Abruf")
    ap.add_argument("--gruppe", metavar="NAME", help="mit --explain: Produktgruppe aus marken.toml (z. B. ssd, ram, gpu)")
    args = ap.parse_args()

    if args.explain:
        explain(args.explain, args.html, args.gruppe)
        return

    if args.test_mail:
        notify("Deal-Watcher: Testmail", "Die Benachrichtigung funktioniert.")
        return

    cfg, queries = load_config()
    state = load_state()
    pause_bis = state.get("zustand", {}).get("pause_bis")
    if pause_bis and dt.datetime.now() < dt.datetime.fromisoformat(pause_bis):
        print(f"Pausiert wegen kleinanzeigen-Sperre bis {pause_bis} – dieser Lauf wird übersprungen.")
        return
    fetcher = Fetcher((float(cfg["pause_min_s"]), float(cfg["pause_max_s"])))
    t_start = time.time()
    hits, stats = run(cfg, queries, state, fetcher)
    print(f"\n{stats['requests']} Seitenabrufe, {stats['listings']} Anzeigen gelesen, "
          f"{len(hits)} Treffer, {time.time() - t_start:.0f} s")

    if hits:
        subject, body = compose(hits)
        if args.dry_run:
            print("\n--- (Testlauf, keine Mail) ---\n" + subject + "\n\n" + body)
        else:
            notify(subject, body, hits[0]["url"])
    handle_block(state, stats, args.dry_run)
    health_check(state, stats, args.dry_run)
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
