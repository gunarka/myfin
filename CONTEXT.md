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
| `app_pension.py` | 🏖️ Altersvorsorge | Vorsorge-Bausteine, Entgeltpunkte-Rechner (gesetzliche Rente), Kapital-Hochrechnung (Zinseszins), Rentenlücken-Rechner |
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
inflation       – jährliche %-Steigerung pro Gruppe (Prognose-Annahme)
inflation_history – historische Referenzwerte je Kategorie (z.B. Destatis-VPI), manueller CSV-Import
pension_plans   – Altersvorsorge-Bausteine (gesetzlich/betrieblich/privat/ETF/Immobilie)
pension_income  – Jahres-Entgelte je Person für den Entgeltpunkte-Rechner (gesetzliche Rente)
```

### inflation_history-Tabelle

```sql
category_code TEXT       -- stabiler Schlüssel, z.B. COICOP-Code "CC13-07223"
category      TEXT       -- Anzeige-Label, z.B. "Autogas und andere Kraftstoffe"
group_code    TEXT       -- übergeordnete Gruppe, z.B. "07" (COICOP-Abteilung)
group_label   TEXT       -- z.B. "Verkehr"
level         INTEGER    -- Gliederungstiefe (2/4/5-Steller bei COICOP)
date          DATE
index_value   DOUBLE     -- VPI-Indexwert, optional
yoy_pct       DOUBLE     -- Veränd. z. Vorjahr in %, wird aus index_value abgeleitet falls leer
PRIMARY KEY (category_code, date)
```

Import in app_forecast.py (Sub-Tab „📈 Inflation“): erkennt automatisch das Destatis-
GENESIS-Online-Flat-File-Format (Spalten `2_variable_attribute_code` u.a.) und leitet
Gliederungsebene (2-/4-/5-Steller), Abteilungs-Zuordnung sowie Jahres-/Monatswerte
direkt aus dem COICOP-Code ab (`_coicop_level`/`_coicop_division` in app_forecast.py,
`COICOP_DIVISIONS`-Lookup in app_functions.py). Für andere CSV-Quellen greift ein
generischer Fallback mit manuellem Spalten-Mapping. Kein automatischer Abruf.
`average_yoy_inflation()` liefert Ø-Werte über N Jahre je `category_code`, die per
Button direkt als Steigerungssatz einer `inflation`-Gruppe übernommen werden können.

### pension_plans-Tabelle

```sql
plan_id          INTEGER PK
person           TEXT
name             TEXT      -- freier Bausteinname, z.B. "Direktversicherung Firma X"
typ              TEXT      -- gesetzlich/betrieblich/Riester/Rürup/ETF-Sparplan/Immobilie/Sonstige
anbieter         TEXT
monatl_beitrag   DOUBLE
aktueller_wert   DOUBLE
erwartete_rente  DOUBLE    -- € / Monat lt. letzter Renteninfo, manuell gepflegt
rendite_pct      DOUBLE    -- angenommene Jahresrendite für Hochrechnung
rentenbeginn     DATE      -- frei wählbar (kein enges Default-Datumsfenster)
status           TEXT      -- aktiv/pausiert/beendet
note             TEXT
```

Eigenständige Tabelle ohne group/category-FK (Bestandsobjekt, keine Buchung). Optionale
Verknüpfung zum Cashflow-Forecast über einen zusätzlichen `recurring`-Eintrag mit
`group="Altersvorsorge"`. Hochrechnung rein lokal per Zinseszins-Formel
(`project_capital`, `required_monthly_saving` in `app_functions.py`) – keine externen
Kurs- oder Rentendaten-Abrufe. In `app_pension.py` (Tab „Verwalten“) gibt es neben der
Tabellen-Bulk-Bearbeitung (`st.data_editor`, nur Feld-Updates) zwei Expander für
Einzel-Operationen: „✏️ Baustein bearbeiten“ (Baustein per Selectbox wählen, alle Felder
inkl. `rentenbeginn` frei editieren) und „🗑️ Baustein entfernen“ (Baustein wählen,
gezielt löschen) – Löschen findet bewusst nicht mehr als Checkbox-Spalte in der Tabelle
statt, sondern in einem eigenen Expander. `rentenbeginn`-Eingaben (Neuanlage,
Einzel-Bearbeitung, Tabellen-Spalte) sind bewusst mit einem weiten Datumsfenster
(`RENTENBEGINN_MIN`/`RENTENBEGINN_MAX`, ca. 1950 bis heute+80 Jahre) versehen, da
Streamlits `date_input` sonst implizit nur „Default ± 10 Jahre“ zulässt. Die Anzeige-
Spaltenreihenfolge wird in `app_pension.py` explizit erzwungen (`name` direkt neben
`person`), da sie bei per `ALTER TABLE` nachträglich ergänzten Spalten (Migration
älterer Datenbanken) physisch am Tabellenende landen und `SELECT *` sonst eine andere
Reihenfolge liefern würde als bei einer frischen Installation.

Tab „Verwalten“ enthält außerdem die **Netto-Berechnung** (Personenauswahl + Ergebnis
des aktiven Szenarios – siehe unten).

### pension_income-Tabelle

```sql
income_id              INTEGER PK
person                 TEXT
jahr                   INTEGER
individuelles_entgelt  DOUBLE    -- eigenes Entgelt bis zur Beitragsbemessungsgrenze
durchschnittsentgelt   DOUBLE    -- statist. Durchschnittsentgelt (Anlage 1 SGB VI), vorbelegt/editierbar
```

Upsert über (Person, Jahr) via `save_pension_income_year()`. `ensure_pension_income_years(person, start_year, end_year)`
füllt fehlende Jahre lückenlos mit 0 € / statist. Durchschnittsentgelt auf (Default-Bereich
`INCOME_START_YEAR` (1995) bis Vorjahr) – bestehende Einträge bleiben unverändert.

Grundlage für den Entgeltpunkte-Rechner in `app_pension.py` (Tab „Gesetzliche Rente“). Das
Formular trennt **Fakten** (Person, Geburtsdatum, aktueller Rentenwert, Jahres-Entgelte) von
**Annahmen zur Entwicklung** (Renteneintritt-Zeitpunkt/Zugangsfaktor, Rentenfaktor,
Rentenwert-Entwicklung, zukünftige Entgeltpunkte/Jahr), die jeweils direkt neben/unter dem
zugehörigen Fakt platziert sind:

```python
berechne_gesetzliche_rente(
    income_df, geburtsdatum, monate_abweichung, zukunfts_ep_rate,
    rentenwert, rentenwert_entwicklung_pct, rentenfaktor,
) -> dict  # entgeltpunkte_df, summe_ep_bisher, ep_letzte5_avg, regelaltersgrenze_datum,
           # rentenbeginn, zugangsfaktor, zukuenftige_ep, gesamt_ep,
           # rentenwert_bei_beginn, rente_monatlich, rente_jaehrlich
```

Rente = Summe Entgeltpunkte (erfasst + linear fortgeschrieben) × Zugangsfaktor ×
Rentenartfaktor × Rentenwert (§§ 63 ff. SGB VI, vereinfacht). Regelaltersgrenze wird
gestaffelt nach Geburtsjahrgang ermittelt (`regelaltersgrenze_zusatz`, 65→67 Jahre).
Zugangsfaktor: −0,3 %/Monat vor, +0,5 %/Monat nach der Regelaltersgrenze
(`zugangsfaktor`, gesteuert über eine Monats-Auswahl). „Zukünftige Entgeltpunkte pro
Jahr“ ist per Default auf den errechneten Ø-Wert der letzten 5 erfassten Jahre
vorbelegt. Durchschnittsentgelt-Referenztabelle (`DURCHSCHNITTSENTGELT_REF`) und
aktueller Rentenwert (`AKTUELLER_RENTENWERT`) sind statisch im Code hinterlegt und in
der UI überschreibbar – kein automatischer Abruf.

**Fakten & Szenarien (Speicherung):** Beides nutzt die vorhandene `scenarios`-Tabelle
mit strukturierten Namen (kein neues Schema):

```python
save_pension_facts(person, {"geburtsdatum": ..., "rentenwert": ...})   # 1 Satz je Person
load_pension_facts(person) -> dict

save_pension_scenario(person, name, {                                  # N Szenarien je Person
    "monate_abweichung": ..., "rentenfaktor_label": ...,
    "rentenwert_entwicklung": ..., "zukunft_ep": ...,
    "grundfreibetrag_entwicklung": ..., "sonstige_abzuege": ...,
    "kvdr_pflichtversichert": ..., "kv_zusatzbeitrag": ..., "pv_kinderlos": ...,
    "status": "aktiv" | "inaktiv",
})
load_pension_scenario(person, name) -> dict | None
list_pension_scenarios(person) -> list[str]
delete_pension_scenario(person, name)

activate_pension_scenario(person, name)         # setzt "aktiv", alle anderen "inaktiv"
get_active_pension_scenario(person) -> str | None
```

Intern: `pension_facts::<Person>` bzw. `pension_scenario::<Person>::<Name>` als
`scenarios.name`. Je Person ist höchstens ein Szenario **aktiv** (Status im
Szenario-Dict, exklusiv über `activate_pension_scenario` gesetzt/gewechselt). So kann
z. B. „Gunar“ die Szenarien „Basis“ (Regelaltersrente, Rentenwert konstant) und
„Frühe Rente“ (48 Monate vorgezogen, Rentenwert +1 %/Jahr) parallel pflegen und im Tab
„Gesetzliche Rente“ per Multiselect vergleichen (Status, Rentenbeginn, Zugangsfaktor,
Entgeltpunkte, Bruttorente). Neu gespeicherte Szenarien starten bewusst **inaktiv**
(kein Auto-Aktivieren) – die Aktivierung/der Wechsel erfolgt ausschließlich im Tab
„Verwalten“ (Szenario-Auswahl + Button „✅ Aktivieren“ innerhalb der Netto-Berechnung);
beim Speichern eines bestehenden Szenarios (gleicher Name) bleibt dessen Status
erhalten. Das aktive Szenario steuert die **Netto-Berechnung im Tab „Verwalten“** (dort
wird nur die Person gewählt, nicht mehr die Annahmen).

### Netto-Berechnung (vereinfachte Einkommensteuer)

```python
berechne_netto_rente(
    rente_brutto_jahr, rentenbeginn_jahr, weitere_alterseinkuenfte_jahr,
    grundfreibetrag_entwicklung_pct, sonstige_abzuege=0.0,
) -> dict  # besteuerungsanteil_pct, steuerpflichtiger_anteil, rentenfreibetrag_eur,
           # grundfreibetrag, zve, einkommensteuer_jahr, brutto_gesamt_jahr,
           # netto_gesamt_jahr/-monat, netto_gesetzliche_rente_jahr/-monat
```

Schätzt die Netto-Rente im ersten vollen Rentenjahr:

- **Besteuerungsanteil** (`besteuerungsanteil_pct`) nach Renteneintrittsjahrgang:
  50 % bis 2005, +2 %-Pkt./Jahr bis 2020 (80 %), +1 %-Pkt./Jahr 2021–2022 (82 %),
  ab 2023 +0,5 %-Pkt./Jahr bis 100 % im Jahr 2058 (Wachstumschancengesetz 03/2024).
- **Grundfreibetrag** (`grundfreibetrag_fuer_jahr`, Tabelle `GRUNDFREIBETRAG_REF`
  2022–2026 amtlich, danach Fortschreibung mit der Annahme
  „Zukünftige Entwicklung Grundfreibetrag p.a.“ aus dem Szenario, Default 2,0 %).
- **Einkommensteuer-Grundtarif** (`einkommensteuer`, § 32a EStG): Referenzformel
  VZ 2025 (`einkommensteuer_grundtarif_2025`), für andere Jahre proportional zum
  Verhältnis des Grundfreibetrags zum Referenzwert (12.096 €) gestreckt –
  vereinfachte Fortschreibung der Tarif-Eckwerte analog zum jährlichen Ausgleich
  der kalten Progression.
- **Werbungskosten-/Sonderausgaben-Pauschbetrag** (102 €/36 €) automatisch abgezogen;
  zusätzlich frei definierbare „Sonstige Abzüge“ (z. B. KV/PV-Beiträge) je Szenario.
- **Weitere Alterseinkünfte**: aktive Vorsorge-Bausteine `typ` ∈ {betrieblich,
  Riester, Rürup} derselben Person werden aus `pension_plans` summiert und als voll
  steuer-/beitragspflichtig einbezogen (erhöhen zvE und damit den Grenzsteuersatz
  sowie die KV/PV-Basis); die resultierenden Abgaben werden anteilig der
  gesetzlichen Rente zugerechnet.
- **Solidaritätszuschlag** (`solidaritaetszuschlag`, §§ 3, 4 SolzG): 5,5 % der
  Einkommensteuer, aber erst oberhalb einer Freigrenze (`SOLI_FREIGRENZE_REF`,
  Einzelveranlagung, amtlich 2022–2026, danach mit der Grundfreibetrag-Annahme
  fortgeschrieben); direkt darüber Milderungszone (max. 11,9 % des Differenz-
  betrags) zur Vermeidung eines Belastungssprungs.
- **Kranken-/Pflegeversicherung** (nur falls „Gesetzlich krankenversichert als
  Rentner (KVdR)“ aktiv): auf die gesetzliche Rente hälftig geteilter Beitrag
  (allgemeiner Satz 14,6 % + Zusatzbeitrag, editierbar, Default 2,9 %) nach
  § 249a SGB V; die Pflegeversicherung (3,6 %, +0,6 %-Punkte für Kinderlose,
  § 59 SGB XI) trägt der Rentner stets allein. Betriebsrenten/Riester/Rürup
  unterliegen oberhalb eines Freibetrags (`BETRIEBSRENTEN_FREIBETRAG_KV_JAHR`)
  dem vollen, ungeteilten Satz (§ 250 Abs. 1 Nr. 1 SGB V). Beitragsbasis
  gedeckelt auf die KV/PV-Beitragsbemessungsgrenze
  (`KV_PV_BEITRAGSBEMESSUNGSGRENZE_JAHR`, 69.750 €/Jahr, 2026). KV/PV-Beiträge
  mindern zusätzlich als Sonderausgaben das zu versteuernde Einkommen (statt
  des Pauschbetrags, falls höher).

Alle Formeln gegen mehrere unabhängige, öffentlich dokumentierte Rechenbeispiele
validiert (Besteuerungsanteil-Tabelle 2005–2060, Rentenfreibetrag-Beispiele 2025/2026,
KV/PV-Beitragsstaffel nach Rentenhöhe, Soli-Milderungszone, Tarif-Stetigkeit an den
Zonengrenzen). Vereinfachtes Modell ohne Kirchensteuer und ohne Zusammenveranlagung
(dafür ggf. „Sonstige Abzüge“ nutzen) – ersetzt keine Steuer-/
Sozialversicherungsberatung.

**UI-Standort:** Die Netto-Berechnung inkl. Szenario-**Aktivierung** ist im Tab
„Verwalten“ verankert (nicht im Tab „Gesetzliche Rente“, dort werden nur Fakten/
Annahmen/Szenario-Inhalte gepflegt und im Vergleich nur read-only angezeigt, welches
Szenario aktiv ist). Im Tab „Verwalten“ wählt man Person + Szenario und aktiviert per
Button; Geburtsdatum, Jahres-Entgelte und die Annahmen des dadurch aktiven Szenarios
werden automatisch geladen. Ohne Szenario, ohne aktives Szenario bzw. ohne hinterlegtes
Geburtsdatum erscheint ein Hinweis mit Verweis auf den Tab „Gesetzliche Rente“. Aktive
Vorsorge-Bausteine `typ` ∈ {betrieblich, Riester, Rürup} derselben Person werden dabei
automatisch als weitere Alterseinkünfte einbezogen.

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
- **DB-Verbindung**: `_connect_duckdb_with_recovery()` in `app_functions.py` fängt ein
  defektes WAL-Log ab (z.B. nach hartem Prozessabbruch/Absturz). DB-Datei + WAL werden
  vor jeder Reparatur unverändert nach `.data/backups/` kopiert, nichts wird destruktiv
  gelöscht; die App läuft danach mit dem Stand des letzten Checkpoints weiter. Hinweis
  dazu wird einmalig via `st.session_state["wal_recovery_notice"]` in `app.py` angezeigt.
- **GitHub**: `.gitignore` schließt `.data/`, DB-Dateien, Keyring und Secrets aus;
  `publish_to_github.py` prüft die gestagten Dateien zusätzlich gegen eine
  Sperrliste, bevor committet/gepusht wird (Details in README.md).

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

**Filter:** Expander „Filter" (Einschließen: Gruppe, Kategorie, Neu, Kontext, Beziehung, Spezial, Zeitraum-Slider, 2 Textsuchfelder; Standard: Beziehung=Familie). Expander „Ausschließen" mit denselben Feldern (Standard-Ausschlüsse: Kontext=Arbeit, Spezial=True).

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
| `berechne_gesetzliche_rente(...)` | Entgeltpunkte-Rechner gesetzliche Rente (siehe pension_income-Tabelle oben) |
| `ensure_pension_income_years(person, start, end)` | Füllt Jahres-Entgelte-Lücken (Default ab 1995) mit 0 €/Referenzwert auf |
| `save_pension_facts` / `load_pension_facts` | Fakten (Geburtsdatum, Rentenwert) je Person |
| `save_pension_scenario` / `load_pension_scenario` / `list_pension_scenarios` / `delete_pension_scenario` | Benannte Annahmen-Szenarien je Person |
| `activate_pension_scenario` / `get_active_pension_scenario` | Aktivierung/Abfrage des einen aktiven Szenarios je Person |
| `berechne_netto_rente(...)` | Netto-Rente: Besteuerungsanteil, Grundfreibetrag, § 32a-Tarif, Soli, KV/PV (KVdR), weitere Alterseinkünfte |
