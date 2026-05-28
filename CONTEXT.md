# MyFin – Projektkontext für Claude Code

## Was ist MyFin?

Lokale Streamlit-App zur persönlichen Finanzverwaltung. Transaktionen kommen per FinTS/HBCI direkt von der Bank (`python-fints`) oder per CSV-Import. Speicherung in einer lokalen DuckDB-Datei (`.data/bookings.duckdb`). Zugangsdaten verschlüsselt im CryptFile-Keyring (`.data/keyring.cfg`).

## Dateistruktur

| Datei | Seite | Zweck |
|---|---|---|
| `app.py` | — | Einstiegspunkt: `set_page_config`, Seitenregistrierung, `f.navigation()` **vor** `pg.run()` (Sidebar-Fix) |
| `app_functions.py` | — | Gemeinsame Logik: Spalten-Definitionen, DB-Verbindung, Keyring, Kategorien-CRUD, Forecast-Engine |
| `app_dashboard.py` | 📊 Analysieren | KPIs, Charts, gefilterte Transaktionsübersicht über alle Konten |
| `app_assign.py` | 🔖 Zuordnen | Transaktionen kategorisieren per `data_editor` (In-Place-Bearbeitung) |
| `app_forecast.py` | 🔮 Vorhersagen | Cashflow-Prognose: wiederkehrende Buchungen, Konfidenzband, Inflation, Szenarien |
| `app_retrieve.py` | 🏦 Importieren | FinTS-Download und CSV-Import mit Duplikatprüfung |
| `app_admin.py` | ⚙️ Administrieren | Konten (Keyring-CRUD), Paketverwaltung, Software-Umgebung |

## Datenbank-Schema (DuckDB)

```
Accounts        – Person, Bank, Konto, IBAN, Abruf (FinTS | CSV)
<IBAN>          – eine Tabelle pro Konto (Schema siehe unten)
categories      – group, category (UNIQUE)
recurring       – wiederkehrende Buchungen für Forecast
oneoff          – einmalige geplante Ereignisse für Forecast
scenarios       – benannte What-If-Szenarien als JSON
inflation       – jährliche %-Steigerung pro Gruppe
```

### Transaktions-Tabelle (pro IBAN)

Wichtige Spalten (`col_*`-Definitionen in `app_functions.py`):

- `row_id`, `date`, `date_year`, `date_month`
- `amount`, `saldo`
- `applicant`, `applicant_name`, `applicant_iban`
- `location`, `purpose`, `posting_text`
- `entry_date`, `guessed_entry_date`
- `group`, `category`, `context`, `relation`
- `note`
- `new_entry` (bool), `special` (bool)
- `bank_reference`, `end_to_end_reference`

### recurring-Tabelle

```sql
forecast_id   INTEGER PK
applicant     TEXT
amount        DOUBLE        -- negativ = Ausgabe
group         TEXT
category      TEXT
relation      TEXT
context       TEXT
iban          TEXT          -- welches Konto bucht ab
interval_type TEXT          -- täglich/wöchentlich/monatlich/quartalsweise/halbjährlich/jährlich
interval_num  INTEGER       -- Alle N Intervalle
start_date    DATE
end_date      DATE          -- NULL = kein Ende
status        TEXT          -- aktiv/pausiert/beendet
variability   DOUBLE        -- Standardabweichung in % für Konfidenzband
note          TEXT
```

## Sicherheits-Konventionen

- **IBANs als Tabellennamen** immer über `safe_table_name(iban)` validieren (DB-Whitelist)
- **SQL** immer parametrisiert (`?`), nie String-Interpolation mit User-Input
- **PINs** niemals loggen, anzeigen oder in Session-State schreiben (nur Keyring)
- **HTTPS** für FinTS-Server erzwingen
- `build_select()` für SELECT-Statements verwenden

## Spalten-Definitionen

Alle Spalten als `col`-Dataclass in `app_functions.py`:

```python
# Transaktionen
col_ctx, col_grp, col_cat, col_rel, col_amt,
col_app, col_anm,          # applicant / applicant_name
col_loc, col_dat, col_da1, col_da2, col_mon, col_yea,
col_inf, col_add, col_brf, col_eer, col_ibn,
col_new, col_rid, col_sld, col_spc, col_note,
# Forecast
col_fid, col_iban, col_int_typ, col_int_num,
col_st_dat, col_en_dat, col_status, col_var_pct, col_note,
col_oid, col_oo_dat
```

Konstanten: `INTERVAL_TYPES`, `STATUS_TYPES`, `MONTH_NAMES` (int→"Jan"…"Dez"), `COL_LABELS` (col→label dict)

## Design-System

Konsistentes Dark-Theme über alle Seiten. Jede Seite definiert `C` und `PLOTLY_THEME` lokal (gleiche Werte):

```python
C = {
    "bg": "#0D0F14", "surface": "#161920", "border": "#252830",
    "text": "#E8EAF0", "muted": "#6B7280",
    "green": "#00E5A0", "red": "#FF4D6A", "blue": "#4D9FFF",
    "amber": "#FFB547", "purple": "#A78BFA",
}
PLOTLY_THEME = dict(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", ...)
```

- Positive Beträge = grün, negative = rot (`colour_amount()`)
- Chart-Höhe einheitlich 420 px (lokale Konstante `CHART_H` bzw. `CHART_HEIGHT` je Datei)
- Chart-Titel als `title=` in `update_layout`, nicht als `st.subheader`

## Seiten-Übersicht

### app_dashboard.py – Analysieren

Kontenübergreifende Datenanalyse. Konto-Auswahl per Schnellbutton (Nur Giro / Alle) und Checkboxen – Giro-Konten links, alle anderen rechts; Giro-Konten standardmäßig aktiviert.

**Filter:** Expander „Filter" (Einschließen: Gruppe/Kategorie, Zeitraum-Slider, 2 Textsuchfelder). Expander „Ausschließen" mit eigenen Multiselects (Standard-Ausschlüsse: Kontext=Arbeit, spezial=True).

**Chart-Filter:** Klick auf Chart-Element schreibt in `st.session_state["chart_filter"]` → alle nachfolgenden Filter/Tabellen reagieren. Reset-Button im Info-Banner hebt den Filter auf. Helper: `_pt(ev)` (ersten Punkt aus Plotly-Event), `_set_cf(new_cf)` (setzt und rerun).

**7 KPIs:** Einnahmen · Ausgaben · Saldo · Sparrate · Ø Monatl. Einnahmen · Ø Monatl. Ausgaben · Kontostand am Ende des gewählten Zeitraums (summiert über alle gewählten Konten).

**Charts (5 Zeilen × 2 Spalten, Breite 3:2):**

| Zeile | Linke Spalte | Rechte Spalte |
|---|---|---|
| 1 | Monatlicher Saldo (Bar + 3M-Rolling-Ø Linie, klickbar → Monatsfilter) | Saldo: Kontext × Beziehung (Group-Bar, klickbar) |
| 2 | Monatliche Einnahmen vs. Ausgaben (Line+Fill, klickbar) | Sunburst Ausgaben Gruppe → Kategorie (klickbar) |
| 3 | Top-10-Ausgaben-Kategorien (Horizontal-Bar, klickbar) | Heatmap Ausgaben-Anteil nach Gruppe (% je Monat) |
| 4 | Saldenverlauf tagesgenau: gestapelt bei mehreren Konten, Füllung bei einem; ffill zwischen Buchungen | Histogramm Transaktionsvolumen (log–log, Einnahmen & Ausgaben überlagert) |
| 5 | Jährlicher Vergleich nach Gruppe (Group-Bar, klickbar) | Top-12-Empfänger nach Ausgaben (Horizontal-Bar, klickbar) |

**Transaktions-Tabelle:** Expander „Alle Transaktionen" mit `st.dataframe` inkl. `col_note`.

Wichtige Funktionen: `list_saved_users`, `inc_filter`, `exc_filter`, `build_select`, `get_config`

### app_assign.py – Zuordnen

Kategorisierung per `st.data_editor`. Konto-Auswahl (Expander, Giro links / Andere rechts, Button-basiert), Filter (Einschließen/Ausschließen), optionales Diagramm. Neue Kategorien und Kontext/Beziehungs-Werte können inline angelegt werden.

### app_forecast.py – Vorhersagen

5 Tabs:

| Tab | Inhalt |
|---|---|
| Verwalten | 5 Sub-Tabs: Wiederkehrend, Einmalig, Auto-Erkennung, Aus Mittelwert, Inflation |
| Vorhersage | Parameter, What-If-Editor, KPIs, Charts (Saldo, Einnahmen/Ausgaben, Drilldown, Sunburst, Heatmap), CSV-Export |
| Warnungen | Monate unter Schwellwert, Chart mit Schwellen-Linie |
| Prognose vs. Ist | Rückwärtsvergleich der Forecast-Konfiguration mit echten Buchungen |
| Szenarien | Parameter unter Namen speichern, Multiselect-Vergleich, Löschen |

Forecast-Engine (`compute_forecast` in `app_functions.py`):

```python
compute_forecast(
    horizon_months,
    overrides,            # {forecast_id: betrag} – What-If wiederkehrend
    oneoff_overrides,     # {oneoff_id: betrag}   – What-If einmalig
    excluded_ids,         # set[forecast_id] – deaktivierte Einträge
    excluded_oneoff_ids,  # set[oneoff_id]   – deaktivierte einmalige
    pct_increase,
    confidence,
    inflation_map,
    include_oneoff,
    only_active,          # nur status='aktiv'
    forecast_start,
) -> {"events": DataFrame, "monthly": DataFrame, "balances_start": dict}
```

`monthly`: `year_month`, `income`, `expense`, `net`, `net_lower`, `net_upper`, `saldo`, `saldo_lower`, `saldo_upper`

Startsaldo = Summe letzter `saldo`-Werte aller in `recurring.iban` vorkommenden Konten.  
Inflation wirkt kumulativ: `amount × (1 + annual_pct/100)^years_elapsed`

### app_retrieve.py – Importieren

Konten werden anhand der `Abruf`-Spalte in der `Accounts`-Tabelle aufgeteilt: `FinTS`-Konten erscheinen im FinTS-Tab, `CSV`-Konten im CSV-Tab.

Tab **FinTS/HBCI**: Konto auswählen, Datumsbereich wählen, Transaktionen herunterladen.  
Tab **CSV-Import**: Datei hochladen, Spalten-Mapping, Duplikatprüfung per `bank_reference`.  
Duplikate werden vor dem DB-Insert erkannt und übersprungen.

### app_admin.py – Administrieren

Tab **Bankkonten**: Keyring-CRUD (Konto hinzufügen, Zugangsdaten anzeigen, entfernen).  
Tab **Software**: Paketverwaltung (installieren, deinstallieren, Snapshot), Aktivitäts-Log, Umgebungsinfo.

## Hilfs-Funktionen in app_functions.py

| Funktion | Zweck |
|---|---|
| `distinct_field_values(field_col)` | Distinct-Werte eines Feldes aus allen Konto-Tabellen |
| `category_average(group, category, months, ...)` | Mittlerer Monatsbetrag für eine Gruppe/Kategorie |
| `liquidity_warnings(monthly, threshold)` | Monate, in denen `saldo_lower` < Schwellwert |
| `forecast_vs_actual(months_back, exclude_special)` | Prognose vs. Ist – rückwärtiger Vergleich |
