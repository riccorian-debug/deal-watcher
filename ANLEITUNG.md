# Deal-Suche ohne Credits und ohne Handy – Einrichtung

Alles wird am PC im Browser eingerichtet. Die Deals kommen als **E-Mail in dein Gmail** –
keine App, keine Push-Nachrichten aufs Handy.

| | Deal-Watcher auf GitHub (Hauptteil) | Suchaufträge bei kleinanzeigen (optional) |
|---|---|---|
| Was | Skript berechnet Marktpreise und prüft Verkäufer | gespeicherte Suchen mit Preisfenster |
| Wie oft | stündlich (7–23 Uhr) | kleinanzeigen verschickt selbst |
| Meldet | nur geprüfte Deals mit Bewertung 1–10, gebündelt in einer Mail pro Lauf | jede neue Anzeige im Preisfenster, ungeprüft |
| Einrichtung | ca. 15 Minuten, einmalig | ca. 10 Minuten |

Wenn du möglichst wenige Mails willst: nur den Deal-Watcher einrichten.

---

## Teil 1: Deal-Watcher auf GitHub (15 Minuten)

### 1. Repository anlegen
1. Auf **github.com** ein kostenloses Konto anlegen.
2. Oben rechts **+ → New repository**, Name `deal-watcher`, Sichtbarkeit **Public** → **Create repository**.

> Public, weil öffentliche Repos unbegrenzt kostenlose Laufzeit haben. Mailadresse und Passwort liegen in
> „Secrets“ und sind nicht sichtbar. Öffentlich sind nur Suchbegriffe und die Merkliste (Anzeigen-IDs).

### 2. Dateien hochladen
1. Im Repo auf **uploading an existing file** klicken.
2. Diese vier Dateien reinziehen: `deal_watcher.py`, `config.toml`, `state.json`, `requirements.txt` → **Commit changes**.
3. **Add file → Create new file**, als Namen genau `.github/workflows/deal-watcher.yml` eintippen
   (die Schrägstriche legen die Ordner an), den Inhalt der Datei `deal-watcher.yml` einfügen → **Commit changes**.

### 3. Gmail-App-Passwort
1. **myaccount.google.com/apppasswords** öffnen (dafür muss die 2-Schritt-Bestätigung aktiv sein).
2. Name `Deal-Watcher` → **Erstellen** → das 16-stellige Passwort kopieren.

### 4. Secrets hinterlegen
Im Repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Wert |
|---|---|
| `SMTP_USER` | `ricco.rian@gmail.com` |
| `SMTP_PASSWORD` | das App-Passwort aus Schritt 3 |

> **Ohne App-Passwort geht es auch:** Schritte 3 und 4 einfach weglassen. Dann legt der Deal-Watcher jeden
> Fund als „Issue“ im Repo an, und GitHub schickt dir dazu automatisch eine Mail an die Adresse deines
> GitHub-Kontos. Nachteil: Die Issues sind in einem öffentlichen Repo für jeden sichtbar.

### 5. Starten
1. Reiter **Actions** öffnen (falls gefragt: „I understand my workflows, go ahead and enable them“).
2. Links **Deal-Watcher** → rechts **Run workflow**.
3. Der erste Lauf dauert ca. 5 Minuten (baut die Preisbasis auf). Danach läuft alles automatisch.
4. Kontrolle: Lauf anklicken → Schritt **Angebote suchen**. Pro Suchbegriff steht dort, wie viele Anzeigen
   gefunden wurden und welcher Marktpreis gilt. Wenn überall „0 Anzeigen“ steht, Claude den Log zeigen.

### Tipp: Deals in einen eigenen Gmail-Ordner
In Gmail oben in der Suchleiste auf das Filter-Symbol → **Betreff:** `Deal:` → **Filter erstellen** →
**Label anwenden: Deals** (neu anlegen). Wenn die Deals nicht im Posteingang auftauchen sollen, zusätzlich
„Posteingang überspringen“ anhaken.

---

## Teil 2: Suchaufträge bei kleinanzeigen (optional, 10 Minuten)

1. Am PC bei **kleinanzeigen.de** einloggen.
2. Die Seite **„Deal-Suchaufträge“** öffnen: https://claude.ai/artifact/KyZuE5hD6iSMfVpRehyQnm
3. Bei jedem Eintrag **Öffnen** → oben über den Ergebnissen **„Suchauftrag speichern“** (Herz mit Lupe).
4. Einmalig unter **Meins → Suchaufträge** prüfen, dass „Mitteilungen aktivieren“ angehakt ist.

Laut kleinanzeigen-Hilfe kommen die Benachrichtigungen dann per E-Mail. Wie oft kleinanzeigen verschickt,
steht dort nicht. Wird es zu viel: in **Meins → Suchaufträge** einzelne Mitteilungen abschalten.

---

## Wie der Deal-Watcher arbeitet

- Er läuft **stündlich von ca. 7 bis 23 Uhr** – bewusst nicht öfter, weil kleinanzeigen IP-Adressen mit zu
  vielen Abrufen sperrt.
- Gemeldet wird eine neue Anzeige oder Preissenkung, wenn sie **mind. 20 % unter dem Marktpreis** liegt und
  **mind. 20 € Spielraum** zum Weiterverkauf lässt (RAM fürs EliteBook: nur die 20 %).
- Vorher prüft er die Anzeigenseite: Kontoalter, Bewertungssiegel, passende Kategorie, Zustand,
  Defekt-Hinweise, Versand. Ab 150 € nur mit etabliertem Verkäufer oder „Direkt kaufen“.
- Alle Funde eines Laufs kommen in **einer** Mail, bester Deal zuerst. Nichts gefunden = keine Mail.
- **Sperre durch kleinanzeigen:** Er erkennt die Sperrseite, pausiert automatisch 6 Stunden (bei erneuter
  Sperre 12, 24, max. 48 Stunden) und macht dann von allein weiter. Erst bei wiederholter Sperre bekommst du
  eine Mail.

## Anpassen

`config.toml` im Browser öffnen → Stift-Symbol → ändern → **Commit changes**.
- Mehr oder weniger Treffer: `min_rabatt_prozent` und `min_spielraum_eur` oben in der Datei.
- Neuen Suchbegriff: einen `[[suche]]`-Block kopieren und anpassen.

## Optional: Popup auf dem PC statt nur Mail

1. Im Browser **ntfy.sh/app** öffnen → **Thema abonnieren** → langen Zufallsnamen wählen,
   z. B. `deals-ricco-7f3k9q2m` (wer den Namen kennt, sieht deine Deals).
2. Benachrichtigungen im Browser erlauben.
3. Denselben Namen im Repo als Secret `NTFY_TOPIC` hinterlegen.

Dann erscheint bei jedem Deal zusätzlich ein Windows-Popup, solange der Browser läuft.

---

## Updates einspielen (wichtig)

Neue Versionen von `deal_watcher.py`, `config.toml` oder `deal-watcher.yml` einfach per
**Add file → Upload files** hochladen (bzw. die yml im Ordner `.github/workflows`), alte Dateien werden ersetzt.

**`state.json` nie erneut hochladen!** Darin sammelt der Deal-Watcher die Marktpreise und merkt sich,
was er schon gemeldet hat. Eine alte Version würde das zurücksetzen.

## Was steht im Protokoll?

Actions → Lauf → suche → **Angebote suchen**:

- `RTX 3080: 43 Anzeigen, 35 passend, Pool 120, Marktpreis ~400 €, neu/billiger 2` – alles in Ordnung.
- `(erster Lauf: nur Preisbasis)` – neue Suche, beim ersten Mal wird nur gesammelt, nichts gemeldet.
- `Diagnose: kein Preis 28x` – kleinanzeigen hat das Seitenlayout geändert → Protokoll an Claude schicken.
- `x … – nur Abholung / Defekt / Konto erst … Tage alt` – Kandidat geprüft und bewusst aussortiert.
- `Zugriff blockiert` – kleinanzeigen sperrt kurz; der Deal-Watcher pausiert automatisch.

Bei Laptops und PCs mischt der Marktpreis verschiedene Ausstattungen (i5/i7, 8/16 GB). Vor dem Kauf
CPU, RAM und SSD kurz mit ähnlichen Anzeigen vergleichen – die Mail weist darauf hin.

## Plan B: auf eigenem Gerät laufen lassen

Falls GitHub dauerhaft gesperrt wird: ein Gerät, das dauerhaft läuft (PC, Raspberry Pi, Homeserver, Python 3.11+).

```bash
git clone https://github.com/DEIN-NAME/deal-watcher.git
cd deal-watcher
pip install -r requirements.txt
python3 deal_watcher.py --dry-run          # Test ohne Mail
```

Mit `crontab -e` eintragen (stündlich, 7–23 Uhr):

```
17 7-23 * * * cd ~/deal-watcher && SMTP_USER=ricco.rian@gmail.com SMTP_PASSWORD=dein-app-passwort python3 deal_watcher.py >> log.txt 2>&1
```

Dann auf GitHub unter **Actions → Deal-Watcher → ... → Disable workflow** den Cloud-Lauf abschalten.
