# MyFin

Lokale Streamlit-App zur persönlichen Finanzverwaltung. Transaktionen werden per FinTS/HBCI direkt von der Bank abgerufen oder per CSV importiert, in einer lokalen DuckDB-Datenbank gespeichert und interaktiv analysiert.

## Features

- **Analysieren** — 7 KPIs, 10 interaktive Plotly-Charts (Saldo, Einnahmen/Ausgaben, Sunburst, Heatmap, Saldenverlauf, Histogramm u. a.) und filterbare Transaktionsübersicht über alle Konten; Klick auf Chart-Elemente setzt den Datenfilter
- **Zuordnen** — Transaktionen interaktiv mit Gruppe/Kategorie/Kontext/Beziehung versehen
- **Vorhersagen** — Cashflow-Prognose auf Basis wiederkehrender Buchungen mit Konfidenzband, Inflation, What-If-Analyse, Szenario-Vergleich und Prognose-vs.-Ist-Auswertung
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
| `.data/bookings.duckdb` | Transaktionen, Kategorien, Forecast-Einträge |
| `.data/keyring.cfg` | Bankzugangsdaten (AES-verschlüsselt) |

## Sicherheitshinweise

- PINs werden ausschließlich im Keyring gespeichert, nie geloggt oder im Session-State abgelegt
- IBANs als Tabellennamen werden gegen eine DB-Whitelist validiert (kein SQL-Injection-Risiko)
- Alle SQL-Abfragen sind parametrisiert
- FinTS-Verbindungen erzwingen HTTPS
