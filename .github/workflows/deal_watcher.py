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

Einstellungen und Suchbegriffe: config.toml. Einrichtung: ANLEITUNG.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
DEFECT_RE = re.compile(
    r"(?:(\w+)\s+)?(defekt|kaputt|bastler|für\s+teile|als\s+ersatzteil|ohne\s+funktion|"
    r"funktioniert\s+nicht|ungetestet|nicht\s+getestet|bildfehler|artefakte)", re.I)
SELF_NEGATING = ("funktioniert", "ungetestet", "nicht", "ohne", "als", "für")
INCOMPLETE_RE = re.compile(
    r"(ohne|keine?|kein)\s+(ssd|festplatte|hdd|akku|netzteil|ram|arbeitsspeicher|display|bildschirm|mainboard|cpu)", re.I)
NEGATIONS = {"nicht", "kein", "keine", "keinerlei", "nie", "keinen", "ohne"}
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


def mentions_defect(text: str) -> bool:
    text = text or ""
    for m in DEFECT_RE.finditer(text):
        prev = (m.group(1) or "").lower()
        word = m.group(2).lower()
        if word.startswith(SELF_NEGATING):
            return True
        if prev in NEGATIONS or prev == "0":
            continue
        if re.match(r"\w*\s+(sektor|sector|pixel|block)", text[m.end():m.end() + 20], re.I):
            continue  # z.B. "defekte Sektoren: 0" – Angabe zu SMART-Werten, kein Defekt
        return True
    return False


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
    desc = soup.select_one("#viewad-description-text")
    d["desc"] = desc.get_text(" ", strip=True) if desc else main
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
    if q.get("kategorie") in ("notebooks", "pcs") and INCOMPLETE_RE.search(t):
        return "unvollständig (ohne SSD/Akku/Netzteil …)"
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


def assess(cand: dict, d: dict, cfg: dict) -> tuple[int | None, list[str], str | None]:
    """Gibt (Punkte-Modifikator, Notizen, Ausschlussgrund) zurueck."""
    notes: list[str] = []
    mod = 0
    age = (today() - d["since"]).days if d["since"] else None
    positive = [b for b in d["badges"] if b in POSITIVE_BADGES]

    if d.get("gesuch"):
        return None, notes, "ist ein Gesuch (jemand sucht), kein Angebot"
    if d["condition"] == "Defekt":
        return None, notes, "Zustand: defekt"
    if mentions_defect(d["desc"]):
        return None, notes, "Beschreibung erwähnt Defekt/ungetestet"
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


def qualify(it: dict, pool: dict, q: dict, cfg: dict) -> dict | None:
    """Prueft eine Anzeige gegen den Marktpreis ihres Pools. Gibt Kandidat oder None zurueck."""
    others = [v[0] for k, v in pool.items() if k != it["id"]]
    if len(others) < cfg["min_vergleiche"]:
        return None
    med = trimmed_median(others)
    disc = (1 - it["price"] / med) * 100
    if it["price"] < med * cfg["zu_billig_prozent"] / 100:
        return None  # verdaechtig billig: falscher Artikel, Zubehoer oder Fake
    own_use = bool(q.get("eigenbedarf", False))
    min_disc = float(q.get("min_rabatt_prozent", cfg["min_rabatt_prozent"]))
    min_margin = 0.0 if own_use else float(q.get("min_spielraum_eur", cfg["min_spielraum_eur"]))
    margin = med - it["price"] - cfg["versand_schaetzung_eur"]
    if disc < min_disc or margin < min_margin:
        return None
    if cfg["nur_mit_versand"] and not it["ship"]:
        return None
    return {**it, "query": q["name"], "kategorie": q.get("kategorie"), "median": med, "n": len(others), "discount": disc, "own_use": own_use}


PENDING_KEYS = ("id", "url", "title", "vb", "ship", "direct", "gesuch", "ort", "datum", "query", "kategorie")


def run(cfg: dict, queries: list[dict], state: dict, fetcher: Fetcher) -> tuple[list[dict], dict]:
    t0 = today()
    run_no = state.get("laeufe", 0) + 1
    state["laeufe"] = run_no
    pools = state.setdefault("pools", {})
    reported = state.setdefault("gemeldet", {})
    pending = state.setdefault("offen", {})
    by_name = {q["name"]: q for q in queries}
    stats = {"blocked": None, "listings": 0, "requests": 0}
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
                c = qualify(it, pool, q, cfg)
                if c:
                    prelim.append(c)
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
            if d["price"] and abs(d["price"] - c["price"]) > 0.5:
                # Preis auf der Anzeigenseite weicht ab -> mit diesem Preis nochmal pruefen
                q_c = by_name.get(c["query"], {})
                c2 = qualify({**c, "price": d["price"]}, pools.get(c["query"], {}), q_c, cfg) if q_c else None
                if not c2 or not (q_c.get("preis_min", 0) <= d["price"] <= q_c.get("preis_max", 10**6)):
                    print(f"  x {c['title'][:60]} – Preis auf Anzeigenseite {fmt_eur(d['price'])} passt nicht")
                    continue
                c.update(price=d["price"], discount=c2["discount"])
            mod, notes, reason = assess(c, d, cfg)
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
            c.update(score=score, notes=notes, ship_cost=d["ship_cost"], condition=d["condition"],
                     margin=c["median"] - c["price"] - ship)
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
        if h.get("kategorie") in ("notebooks", "pcs"):
            lines.append("   Achtung: Marktpreis mischt Ausstattungen – CPU/RAM/SSD mit ähnlichen Anzeigen vergleichen")
        if h["own_use"]:
            lines.append("   Eigenbedarf (EliteBook-Upgrade) – passt: DDR4 SO-DIMM bzw. M.2 NVMe laut Titel")
        else:
            lines.append(f"   Spielraum bei Weiterverkauf zum Marktpreis: ca. {fmt_eur(max(h['margin'], 0))}")
        lines.append(f"   Verkäufer: {'; '.join(h['notes'])}")
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Deal-Watcher für kleinanzeigen.de")
    ap.add_argument("--dry-run", action="store_true", help="keine Mail, state.json nicht speichern")
    ap.add_argument("--test-mail", action="store_true", help="nur Testmail senden")
    args = ap.parse_args()

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
