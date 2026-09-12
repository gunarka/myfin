# MyFin

Lokale Streamlit-App zur persönlichen Finanzverwaltung. Transaktionen werden per FinTS/HBCI direkt von der Bank abgerufen oder per CSV importiert, in einer lokalen DuckDB-Datenbank gespeichert und interaktiv analysiert.

## Features

- **Analysieren** — 7 KPIs, 10 interaktive Plotly-Charts (Saldo, Einnahmen/Ausgaben, Sunburst, Heatmap, Saldenverlauf, Histogramm u. a.) und filterbare Transaktionsübersicht über alle Konten; Klick auf Chart-Elemente setzt den Datenfilter
- **Zuordnen** — Transaktionen interaktiv mit Gruppe/Kategorie/Kontext/Beziehung versehen
- **Vorhersagen** — Cashflow-Prognose auf Basis wiederkehrender Buchungen mit Konfidenzband, Inflation, What-If-Analyse, Szenario-Vergleich und Prognose-vs.-Ist-Auswertung
- **Altersvorsorge** — Vorsorge-Bausteine (gesetzlich/betrieblich/privat/ETF/Immobilie), Entgeltpunkte-Rechner für die gesetzliche Rente mit Fakten/Annahmen-Trennung, je Person frei definierbaren Szenarien (z. B. „Basis“ vs. „Frühe Rente“) und einer Netto-Berechnung (Besteuerungsanteil, Grundfreibetrag, Einkommensteuer-Tarif, Solidaritätszuschlag, Kranken-/Pflegeversicherung für KVdR-Rentner, Betriebsrenten u. a.), Kapital-Hochrechnung per Zinseszins und Rentenlücken-Rechner
- **Importieren** — FinTS-Download oder CSV-Import mit automatischer Duplikatprüfung
- **Administrieren** — Kontoverwaltung und verschlüsselter Keyring für Zugangsdaten

## Voraussetzungen

- Python ≥ 3.11
- Zugang zu einem FinTS/HBCI-fähigen Konto (optional, CSV-Import funktioniert ohne)

## Installation

```bash
pip install -r requirements.txt
```

## Starten

```bash
streamlit run app.py
```

Beim ersten Start wird ein Master-Passwort gesetzt, das den Keyring mit den Bankzugangsdaten verschlüsselt.

## Datenspeicherung

Alle Daten bleiben lokal:

| Pfad | Inhalt |
|---|---|
| `.data/bookings.duckdb` | Transaktionen, Kategorien, Forecast-Einträge, Altersvorsorge-Daten |
| `.data/keyring.cfg` | Bankzugangsdaten (AES-verschlüsselt) |
| `.data/backups/` | Automatische Sicherungen bei DB-Reparatur (siehe unten) |

Nach einem harten Programmabbruch (Absturz, Kill, Stromausfall) kann das
Write-Ahead-Log der Datenbank defekt sein. Die App erkennt das beim Start,
sichert DB-Datei und Log unverändert nach `.data/backups/` und setzt die
Datenbank auf den letzten Checkpoint zurück; ein Hinweis erscheint dann oben
in der App. Es gehen dabei höchstens Änderungen seit dem letzten Checkpoint
verloren, nie ältere Daten.

## Sicherheitshinweise

- PINs werden ausschließlich im Keyring gespeichert, nie geloggt oder im Session-State abgelegt
- IBANs als Tabellennamen werden gegen eine DB-Whitelist validiert (kein SQL-Injection-Risiko)
- Alle SQL-Abfragen sind parametrisiert
- FinTS-Verbindungen erzwingen HTTPS

## Auf GitHub veröffentlichen

Sensible Dateien (`.data/`, DB-Dateien, Keyring, Secrets) sind über
`.gitignore` ausgeschlossen. `publish_to_github.py` legt das Repo per
GitHub-API an, prüft vor jedem Commit erneut, dass keine sensiblen Dateien
gestaged sind, und pusht den Code.

```bash
export GITHUB_TOKEN=ghp_xxx   # Token mit Scope "repo", nie einchecken
python publish_to_github.py --name myfin --private
```

## Bekannte Einschränkung

DuckDB erlaubt pro Datei nur eine schreibende Verbindung. Bitte die App nur in
einem Browser-Tab gleichzeitig öffnen – mehrere parallele Tabs/Sessions können
zu Lock-Konflikten auf `.data/bookings.duckdb` führen.
