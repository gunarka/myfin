"""
app_functions.py
Gemeinsame Hilfsfunktionen, Konfiguration und Datenbankverbindung.
Enthält: Spalten-Definitionen, Keyring-Verwaltung, DB-Verbindung, Navigation.
"""

import json
import re
import pandas as pd
import streamlit as st
from dataclasses import dataclass, field
from datetime import date
import os
import signal
import time
import logging
from keyrings.cryptfile.cryptfile import CryptFileKeyring
import keyring
from pathlib import Path
import duckdb

# ── Logging ──────────────────────────────────────────────────────────────────
# Kein Logging sensibler Daten (PINs, Passwörter, IBANs) – nur Info/Fehler-Ebene
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def colour_amount(val: float) -> str:
    """Zellenfarbe je nach Vorzeichen des Betrags."""
    color = "#00E5A0" if val >= 0 else "#FF4D6A"
    return f"color: {color}"


def get_known_ibans() -> set[str]:
    """
    Gibt die Menge aller bekannten IBANs aus der Accounts-Tabelle zurück.
    Wird als Whitelist für Tabellennamen in SQL-Abfragen verwendet.
    Ergebnis wird pro Session gecacht; Cache-Invalidierung über
    st.session_state.pop("known_ibans_cache", None) nach Konto-Änderungen.
    """
    if "known_ibans_cache" not in st.session_state:
        try:
            rows = con.sql('SELECT "IBAN" FROM "Accounts"').fetchall()
            st.session_state["known_ibans_cache"] = {row[0] for row in rows}
        except Exception:
            st.session_state["known_ibans_cache"] = set()
    return st.session_state["known_ibans_cache"]


def safe_table_name(iban: str) -> str:
    """
    Prüft eine IBAN gegen die DB-Whitelist bekannter Konten.
    Wirft ValueError wenn die IBAN unbekannt oder syntaktisch ungültig ist.
    SICHERHEIT: Verhindert SQL-Injection über manipulierte Tabellennamen.
    """
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{13,32}", iban.strip()):
        raise ValueError(f"Ungültiges IBAN-Format: {iban!r}")
    known = get_known_ibans()
    if iban not in known:
        raise ValueError(f"Unbekannte IBAN (nicht in Accounts-Tabelle): {iban!r}")
    return iban


def build_select(cols: list, table: str) -> str:
    """
    Erzeugt ein SELECT-Statement mit validiertem Tabellennamen.
    SICHERHEIT: `table` wird gegen die DB-Whitelist geprüft (safe_table_name).
                Ausnahme: interne Tabellen wie 'Accounts' werden direkt gequotet.
    """
    col_names = ", ".join(f'"{c}"' for c in cols)
    # Interne Tabellen (Großbuchstaben, kein IBAN-Muster) direkt durchlassen
    if re.fullmatch(r"[A-Za-z_]+", table):
        safe = table   # z.B. "Accounts" – keine IBAN, kein User-Input
    else:
        safe = safe_table_name(table)
    return f'SELECT {col_names} FROM "{safe}"'


def get_config(cols: list) -> dict:
    """Gibt column_config für die übergebenen Spalten zurück."""
    return {c.col: c.typ(label=c.lab, **c.cfg) for c in cols}


# ── Spalten-Definitionen ──────────────────────────────────────────────────────

@dataclass
class col:
    """Beschreibt eine Datenbankspalte mit Anzeigekonfiguration."""
    col: str
    lab: str
    cfg: dict = field(default_factory=dict)
    typ: any = st.column_config.Column
    grp: list[str] = field(default_factory=list)


col_ctx = col("context",          "Kontext",            {"width": "small"})
col_grp = col("group",            "Gruppe")
col_cat = col("category",         "Kategorie")
col_rel = col("relation",         "Beziehung",          {"width": "small"})
col_amt = col("amount",           "Betrag",             {"width": "small"})
col_app = col("applicant",        "Empfänger",          {"width": "medium"})
col_anm = col("applicant_name",   "Empfänger",          {"width": "medium"})
col_loc = col("location",         "Ort",                {"width": "small"})
col_dat = col("date",             "Datum",              {"width": "small", "format": "DD.MM.YYYY"}, st.column_config.DateColumn)
col_da1 = col("entry_date",       "Buchungsdatum")
col_da2 = col("guessed_entry_date","Buchungsdatum2")
col_mon = col("date_month",       "Monat")
col_yea = col("date_year",        "Jahr")
col_inf = col("purpose",          "Verwendungszweck",   {"width": "medium"})
col_add = col("posting_text",     "Art",                {"width": "medium"})
col_brf = col("bank_reference",   "Bank Referenz")
col_eer = col("end_to_end_reference", "Ende-zu-Ende Referenz")
col_ibn = col("applicant_iban",   "IBAN")
col_new = col("new_entry",        "neu")
col_rid = col("row_id",           "Zeile")
col_sld = col("saldo",            "Saldo")
col_spc = col("special",          "spezial")

# ── Forecast-Spalten ──────────────────────────────────────────────────────────
col_fid     = col("forecast_id",    "ID")
col_iban    = col("iban",           "Konto",        {"width": "small"})
col_int_typ = col("interval_type",  "Intervall",    {"width": "small"})
col_int_num = col("interval_num",   "Alle",         {"width": "small"})
col_st_dat  = col("start_date",     "Start",        {"width": "small", "format": "DD.MM.YYYY"}, st.column_config.DateColumn)
col_en_dat  = col("end_date",       "Ende",         {"width": "small", "format": "DD.MM.YYYY"}, st.column_config.DateColumn)
col_status  = col("status",         "Status",       {"width": "small"})
col_var_pct = col("variability",    "± %",          {"width": "small"})
col_note    = col("note",           "Notiz",        {"width": "medium"})

# ── Einmalige Ereignisse ──────────────────────────────────────────────────────
col_oid     = col("oneoff_id",      "ID")
col_oo_dat  = col("event_date",     "Datum",        {"width": "small", "format": "DD.MM.YYYY"}, st.column_config.DateColumn)

INTERVAL_TYPES = ["täglich", "wöchentlich", "monatlich", "quartalsweise", "halbjährlich", "jährlich"]
STATUS_TYPES   = ["aktiv", "pausiert", "beendet"]

COL_LABELS: dict[str, str] = {c.col: c.lab for c in [
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app, col_anm,
    col_loc, col_dat, col_da1, col_da2, col_mon, col_yea, col_inf,
    col_add, col_brf, col_eer, col_ibn, col_new, col_rid, col_sld, col_spc,
    col_fid, col_iban, col_int_typ, col_int_num,
    col_st_dat, col_en_dat, col_status, col_var_pct, col_note,
    col_oid, col_oo_dat,
]}


# ── Pfade & Konstanten ────────────────────────────────────────────────────────

DATA_DIR     = Path(__file__).parent / ".data"
KEYRING_PATH = DATA_DIR / "keyring.cfg"
DB_PATH      = DATA_DIR / "bookings.duckdb"

# Pflichtfelder für FinTS-Zugangsdaten
FIELDS = ["name", "bank", "typ", "bank_account", "bank_identifier",
          "user_id", "pin", "server", "pid"]

FIELD_LABELS = {
    "account":          "Name_Bank_Kontotyp",
    "name":             "Person",
    "bank":             "Bank",
    "typ":              "Kontotyp",
    "bank_account":     "IBAN",
    "bank_identifier":  "BLZ",
    "user_id":          "User ID",
    "pin":              "PIN",
    "server":           "FinTS Server URL",
    "pid":              "Bafin Programm ID",
}


# ── Zugangsdaten-Dataclass ────────────────────────────────────────────────────

@dataclass
class FintsCredentials:
    account:         str
    name:            str
    bank:            str
    typ:             str
    bank_account:    str
    bank_identifier: str
    user_id:         str
    pin:             str   # Wird nie geloggt oder angezeigt
    server:          str
    pid:             str


# ── Keyring / Master-Passwort ─────────────────────────────────────────────────

def init_keyring(master_password: str) -> None:
    """
    Keyring mit verschlüsselter Datei initialisieren.
    SICHERHEIT: Das Master-Passwort wird nur im session_state gehalten,
    nie in Logs oder Umgebungsvariablen geschrieben.
    """
    kr = CryptFileKeyring()
    kr.file_path   = str(KEYRING_PATH)
    kr.keyring_key = master_password
    keyring.set_keyring(kr)


def require_master_password() -> None:
    """
    Sicherheits-Gate: Stoppt die App wenn kein gültiges Master-Passwort
    im session_state vorhanden ist. Zeigt Login-Formular.
    SICHERHEIT: Fehlermeldung gibt keinen Hinweis ob Passwort oder Datei falsch.
    """
    if "master_password" not in st.session_state:
        st.title("🔐 Entsperren")
        with st.form("unlock_form"):
            pw        = st.text_input("Master-Passwort", type="password")
            submitted = st.form_submit_button("Entsperren", width="stretch")

        if submitted:
            if not pw:
                st.error("Bitte Passwort eingeben.")
            else:
                try:
                    init_keyring(pw)
                    # Testlesen um Passwort-Korrektheit zu prüfen
                    keyring.get_password("__test__", "__test__")
                    st.session_state["master_password"] = pw
                    log.info("Master-Passwort akzeptiert.")
                    st.rerun()
                except Exception:
                    # Kein Stack-Trace dem User zeigen (Sicherheit)
                    st.error("❌ Falsches Passwort oder beschädigte Keyring-Datei.")
        st.stop()

    # Keyring bei jedem Rerun neu initialisieren (Streamlit re-importiert Module)
    init_keyring(st.session_state["master_password"])


def logout() -> None:
    """Session löschen und Prozess beenden (sperrt Keyring-Datei)."""
    st.session_state.pop("master_password", None)
    log.info("Abmeldung durchgeführt.")
    time.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)


# ── Datenbankverbindung ───────────────────────────────────────────────────────
# Verbindung wird einmalig pro Session erstellt und wiederverwendet.
# SICHERHEIT: Nur lokale Datei, kein Netzwerk-Endpoint.

if "con" not in st.session_state:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state.con = duckdb.connect(str(DB_PATH))

con: duckdb.DuckDBPyConnection = st.session_state.con

# Guard: Verbindung in schlechtem Zustand (z.B. pending query) → neu verbinden
try:
    con.execute("SELECT 1")
except Exception:
    st.session_state.con = duckdb.connect(str(DB_PATH))
    con = st.session_state.con

# Kern-Tabellen sicherstellen (relevant bei Erststart nach git-Clone)
try:
    con.execute("""
        CREATE TABLE IF NOT EXISTS Accounts (
            "Person" TEXT NOT NULL,
            "Bank"   TEXT NOT NULL,
            "Konto"  TEXT NOT NULL,
            "IBAN"   TEXT NOT NULL UNIQUE,
            "Abruf"  TEXT NOT NULL DEFAULT 'FinTS'
        )
    """)
except Exception:
    pass

# Migration: note-Spalte in allen IBAN-Transaktionstabellen ergänzen
# Wird nur einmalig pro Session ausgeführt (ALTER TABLE IF NOT EXISTS ist idempotent,
# aber das spart unnötige Queries bei jedem Rerun).
if not st.session_state.get("_migration_done"):
    try:
        _ibans = [r[0] for r in con.execute('SELECT "IBAN" FROM "Accounts"').fetchall()]
        for _iban in _ibans:
            con.execute(f'ALTER TABLE "{_iban}" ADD COLUMN IF NOT EXISTS "note" TEXT')
    except Exception:
        pass
    st.session_state["_migration_done"] = True


# ── Keyring CRUD ──────────────────────────────────────────────────────────────

def save_fints_credentials(creds: FintsCredentials) -> None:
    """
    Speichert Zugangsdaten verschlüsselt im Keyring.
    SICHERHEIT: PIN wird nie geloggt.
    """
    service = f"fints:{creds.account}"
    for f_name in FIELDS:
        keyring.set_password(service, f_name, getattr(creds, f_name))
    log.info("Zugangsdaten gespeichert für: %s", creds.account)


def load_fints_credentials(account: str) -> FintsCredentials | None:
    """Lädt Zugangsdaten aus dem Keyring; gibt None zurück wenn unvollständig."""
    service = f"fints:{account}"
    values  = {f_name: keyring.get_password(service, f_name) for f_name in FIELDS}
    if any(v is None for v in values.values()):
        log.warning("Unvollständige Zugangsdaten für: %s", account)
        return None
    return FintsCredentials(account=account, **values)


def delete_fints_credentials(account: str) -> None:
    """Löscht alle gespeicherten Felder eines Accounts aus dem Keyring."""
    service = f"fints:{account}"
    for f_name in FIELDS:
        try:
            keyring.delete_password(service, f_name)
        except keyring.errors.PasswordDeleteError:
            pass
    log.info("Zugangsdaten gelöscht für: %s", account)


# ── Kategorien-Tabelle ────────────────────────────────────────────────────────

def ensure_categories_table() -> None:
    """
    Legt die Tabelle 'categories' an falls sie noch nicht existiert.
    Wird beim ersten Zugriff automatisch aufgerufen.
    Schema: group TEXT NOT NULL, category TEXT NOT NULL, UNIQUE(group, category)
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            "group"    TEXT NOT NULL,
            "category" TEXT NOT NULL,
            UNIQUE ("group", "category")
        )
    """)


def load_categories() -> pd.DataFrame:
    """
    Lädt alle Gruppen/Kategorien aus der categories-Tabelle.
    Gibt einen DataFrame mit Spalten 'group' und 'category' zurück,
    sortiert nach group, category.
    Legt die Tabelle automatisch an wenn sie noch nicht existiert.
    """
    ensure_categories_table()
    return con.execute("""
        SELECT "group", "category"
        FROM categories
        ORDER BY "group", "category"
    """).df().fillna("")


def save_category(group: str, category: str) -> bool:
    """
    Fügt eine neue Gruppe/Kategorie-Kombination in die categories-Tabelle ein.
    Gibt True zurück wenn eingefügt, False wenn bereits vorhanden (UNIQUE-Konflikt).
    Beide Felder müssen nicht-leer sein.
    """
    if not group.strip() or not category.strip():
        raise ValueError("Gruppe und Kategorie dürfen nicht leer sein.")
    ensure_categories_table()
    try:
        con.execute(
            'INSERT INTO categories ("group", "category") VALUES (?, ?)',
            [group.strip(), category.strip()],
        )
        return True
    except Exception:
        return False   # UNIQUE-Konflikt – bereits vorhanden


# ── Account-Liste ─────────────────────────────────────────────────────────────

def list_saved_users():
    """
    Gibt alle Konten aus der Accounts-Tabelle als DataFrame zurück.
    KEY  = interner Schlüssel mit _ (Keyring-kompatibel, nicht ändern)
    LABEL = Anzeigename mit · für die UI
    """
    accounts = con.sql(
        build_select(["Person", "Bank", "Konto", "IBAN", "Abruf"], "Accounts")
    ).df()
    accounts["KEY"]   = accounts["Person"] + "_" + accounts["Bank"] + "_" + accounts["Konto"]
    accounts["LABEL"] = accounts["Person"] + " · " + accounts["Bank"] + " · " + accounts["Konto"]
    return accounts


# ── Forecast: Tabellen-Setup ──────────────────────────────────────────────────

def ensure_forecast_tables() -> None:
    """
    Legt alle Tabellen für die Prognosefunktion an, falls noch nicht vorhanden.
    Schemas:
      - recurring:  wiederkehrende Buchungen
      - oneoff:     einmalige geplante Ereignisse
      - scenarios:  benannte What-If-Szenarien (JSON mit Parametern)
      - inflation:  jährlicher %-Aufschlag pro Gruppe
    """
    con.execute("CREATE SEQUENCE IF NOT EXISTS forecast_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS recurring (
            "forecast_id"   INTEGER PRIMARY KEY DEFAULT nextval('forecast_id_seq'),
            "applicant"     TEXT,
            "amount"        DOUBLE NOT NULL,
            "group"         TEXT,
            "category"      TEXT,
            "relation"      TEXT,
            "context"       TEXT,
            "iban"          TEXT,
            "interval_type" TEXT NOT NULL,
            "interval_num"  INTEGER NOT NULL DEFAULT 1,
            "start_date"    DATE NOT NULL,
            "end_date"      DATE,
            "status"        TEXT NOT NULL DEFAULT 'aktiv',
            "variability"   DOUBLE NOT NULL DEFAULT 10.0,
            "note"          TEXT
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS oneoff_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS oneoff (
            "oneoff_id"  INTEGER PRIMARY KEY DEFAULT nextval('oneoff_id_seq'),
            "applicant"  TEXT,
            "amount"     DOUBLE NOT NULL,
            "group"      TEXT,
            "category"   TEXT,
            "relation"   TEXT,
            "context"    TEXT,
            "iban"       TEXT,
            "event_date" DATE NOT NULL,
            "note"       TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scenarios (
            "name"        TEXT PRIMARY KEY,
            "params_json" TEXT NOT NULL,
            "created_at"  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inflation (
            "group"      TEXT PRIMARY KEY,
            "annual_pct" DOUBLE NOT NULL DEFAULT 0.0
        )
    """)


# ── Forecast: CRUD wiederkehrende Buchungen ───────────────────────────────────

def load_recurring(active_only: bool = False) -> pd.DataFrame:
    ensure_forecast_tables()
    where = "WHERE status = 'aktiv'" if active_only else ""
    return con.execute(f'SELECT * FROM recurring {where} ORDER BY "group", "category", applicant').df()


def save_recurring(rec: dict) -> int:
    """Fügt einen wiederkehrenden Eintrag ein und liefert die neue ID."""
    ensure_forecast_tables()
    cols = ["applicant", "amount", "group", "category", "relation", "context",
            "iban", "interval_type", "interval_num", "start_date", "end_date",
            "status", "variability", "note"]
    vals = [_py(rec.get(c)) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_str = ", ".join(f'"{c}"' for c in cols)
    new_id = con.execute(
        f'INSERT INTO recurring ({col_str}) VALUES ({placeholders}) RETURNING forecast_id',
        vals,
    ).fetchone()[0]
    return new_id


def _py(v):
    """Konvertiert numpy-Skalare in native Python-Typen für DuckDB."""
    if hasattr(v, "item"):
        return v.item()
    return v


_RECURRING_COLS = frozenset([
    "applicant", "amount", "group", "category", "relation", "context",
    "iban", "interval_type", "interval_num", "start_date", "end_date",
    "status", "variability", "note",
])


def update_recurring(forecast_id: int, fields: dict) -> None:
    """Aktualisiert einzelne Felder eines Eintrags."""
    if not fields:
        return
    unknown = set(fields.keys()) - _RECURRING_COLS
    if unknown:
        raise ValueError(f"Unbekannte Spalten für recurring: {unknown}")
    set_clause = ", ".join(f'"{k}" = ?' for k in fields.keys())
    con.execute(
        f'UPDATE recurring SET {set_clause} WHERE forecast_id = ?',
        [_py(v) for v in fields.values()] + [int(forecast_id)],
    )


def delete_recurring(forecast_id: int) -> None:
    con.execute('DELETE FROM recurring WHERE forecast_id = ?', [forecast_id])


# ── Forecast: CRUD einmalige Ereignisse ───────────────────────────────────────

def load_oneoff() -> pd.DataFrame:
    ensure_forecast_tables()
    return con.execute('SELECT * FROM oneoff ORDER BY event_date').df()


def save_oneoff(ev: dict) -> int:
    ensure_forecast_tables()
    cols = ["applicant", "amount", "group", "category", "relation", "context", "iban", "event_date", "note"]
    vals = [_py(ev.get(c)) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_str = ", ".join(f'"{c}"' for c in cols)
    return con.execute(
        f'INSERT INTO oneoff ({col_str}) VALUES ({placeholders}) RETURNING oneoff_id',
        vals,
    ).fetchone()[0]


_ONEOFF_COLS = frozenset([
    "applicant", "amount", "group", "category", "relation", "context", "iban", "event_date", "note",
])

def update_oneoff(oneoff_id: int, fields: dict) -> None:
    if not fields:
        return
    unknown = set(fields.keys()) - _ONEOFF_COLS
    if unknown:
        raise ValueError(f"Unbekannte Spalten für oneoff: {unknown}")
    set_clause = ", ".join(f'"{k}" = ?' for k in fields.keys())
    con.execute(
        f'UPDATE oneoff SET {set_clause} WHERE oneoff_id = ?',
        list(fields.values()) + [oneoff_id],
    )


def delete_oneoff(oneoff_id: int) -> None:
    con.execute('DELETE FROM oneoff WHERE oneoff_id = ?', [oneoff_id])


# ── Forecast: Inflation pro Gruppe ────────────────────────────────────────────

def load_inflation() -> pd.DataFrame:
    ensure_forecast_tables()
    return con.execute('SELECT * FROM inflation ORDER BY "group"').df()


def upsert_inflation(group: str, annual_pct: float) -> None:
    ensure_forecast_tables()
    con.execute(
        'INSERT OR REPLACE INTO inflation ("group", annual_pct) VALUES (?, ?)',
        [group, annual_pct],
    )


# ── Forecast: Szenarien speichern/laden ───────────────────────────────────────

def list_scenarios() -> list[str]:
    ensure_forecast_tables()
    rows = con.execute('SELECT name FROM scenarios ORDER BY name').fetchall()
    return [r[0] for r in rows]


def save_scenario(name: str, params: dict) -> None:
    ensure_forecast_tables()
    con.execute(
        'INSERT OR REPLACE INTO scenarios (name, params_json, created_at) '
        'VALUES (?, ?, CURRENT_TIMESTAMP)',
        [name, json.dumps(params)],
    )


def load_scenario(name: str) -> dict | None:
    ensure_forecast_tables()
    row = con.execute('SELECT params_json FROM scenarios WHERE name = ?', [name]).fetchone()
    return json.loads(row[0]) if row else None


def delete_scenario(name: str) -> None:
    con.execute('DELETE FROM scenarios WHERE name = ?', [name])


# ── Forecast: Auto-Erkennung wiederkehrender Buchungen ────────────────────────
# Reine lokale Analyse auf den DuckDB-Daten – keine externen Aufrufe.
# Heuristik:
#   1. Pro Empfänger × IBAN × Vorzeichen: alle Transaktionen der letzten N Monate
#   2. Median der Tagesabstände → Intervall-Typ ableiten
#   3. Variationskoeffizient des Betrags → Variabilität in %
#   4. Mindestens N Vorkommen erforderlich

def detect_recurring(lookback_months: int = 12, min_occurrences: int = 3) -> pd.DataFrame:
    """
    Durchsucht alle Konto-Tabellen nach Buchungs-Mustern.
    Liefert DataFrame mit Vorschlägen.
    """
    ensure_forecast_tables()

    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(months=lookback_months)
    ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()

    frames = []
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            continue
        try:
            df = con.execute(f"""
                SELECT
                    "{col_app.col}"  AS applicant,
                    "{col_amt.col}"  AS amount,
                    "{col_dat.col}"  AS date,
                    "{col_grp.col}"  AS "group",
                    "{col_cat.col}"  AS category
                FROM "{safe}"
                WHERE "{col_dat.col}" >= ?
                  AND "{col_app.col}" IS NOT NULL
                  AND "{col_amt.col}" IS NOT NULL
            """, [str(cutoff.date())]).df()
        except Exception:
            continue
        if not df.empty:
            df["iban"] = iban
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    all_tx = pd.concat(frames, ignore_index=True)
    all_tx["date"] = pd.to_datetime(all_tx["date"])
    # Sign-stabile Gruppierung: Einnahmen und Ausgaben desselben Empfängers separat
    all_tx["sign"] = all_tx["amount"].apply(lambda x: "+" if x >= 0 else "-")

    suggestions = []
    for (applicant, iban, _sign), grp in all_tx.groupby(["applicant", "iban", "sign"]):
        if len(grp) < min_occurrences:
            continue
        grp = grp.sort_values("date")
        diffs = grp["date"].diff().dt.days.dropna()
        if diffs.empty:
            continue
        median_gap = float(diffs.median())
        if   25 <= median_gap <= 35:    itype, inum = "monatlich",     1
        elif 6  <= median_gap <= 8:     itype, inum = "wöchentlich",   1
        elif 13 <= median_gap <= 16:    itype, inum = "wöchentlich",   2
        elif 85 <= median_gap <= 95:    itype, inum = "quartalsweise", 1
        elif 175 <= median_gap <= 190:  itype, inum = "halbjährlich",  1
        elif 355 <= median_gap <= 380:  itype, inum = "jährlich",      1
        else:
            continue   # Kein klares Muster

        amount_median = float(grp["amount"].median())
        _std          = grp["amount"].std()
        amount_std    = 0.0 if pd.isna(_std) else float(_std)
        variability_pct = (abs(amount_std / amount_median) * 100) if amount_median else 0
        most_grp = grp["group"].mode().iat[0]    if not grp["group"].mode().empty    else None
        most_cat = grp["category"].mode().iat[0] if not grp["category"].mode().empty else None

        suggestions.append({
            "applicant":       applicant,
            "amount":          round(amount_median, 2),
            "iban":            iban,
            "group":           most_grp,
            "category":        most_cat,
            "interval_type":   itype,
            "interval_num":    inum,
            "occurrences":     int(len(grp)),
            "variability_pct": round(min(variability_pct, 100), 1),
            "last_seen":       grp["date"].max().date(),
        })

    if not suggestions:
        return pd.DataFrame()
    return pd.DataFrame(suggestions).sort_values(
        ["occurrences", "amount"], ascending=[False, True]
    ).reset_index(drop=True)


def distinct_field_values(field_col: str) -> list[str]:
    """Gibt sortierte Distinct-Werte eines Feldes aus allen Konten-Tabellen zurück."""
    if field_col not in COL_LABELS:
        raise ValueError(f"Unbekannte Spalte: {field_col!r}")
    ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()
    vals: set[str] = set()
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            continue
        try:
            rows = con.execute(
                f'SELECT DISTINCT "{field_col}" FROM "{safe}" WHERE "{field_col}" IS NOT NULL'
            ).df()[field_col].tolist()
            vals.update(str(v) for v in rows if v)
        except Exception:
            continue
    return sorted(vals, key=str.lower)


def category_average(
    group: str,
    category: str | None,
    months: int = 6,
    relation: str | None = None,
    context: str | None = None,
    special: bool | None = None,
) -> float:
    """Mittlerer Monatsbetrag für eine Gruppe, optional gefiltert auf Kategorie/Relation/Kontext/Spezial."""
    _today = date.today()
    cutoff = pd.Timestamp(_today.replace(day=1)) - pd.DateOffset(months=months - 1)
    ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()
    totals = []
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            continue
        try:
            conditions = [f'"{col_grp.col}" = ?', f'"{col_dat.col}" >= ?']
            params: list = [group, str(cutoff.date())]
            if category:
                conditions.append(f'"{col_cat.col}" = ?')
                params.append(category)
            if relation:
                conditions.append(f'"{col_rel.col}" = ?')
                params.append(relation)
            if context:
                conditions.append(f'"{col_ctx.col}" = ?')
                params.append(context)
            if special is not None:
                conditions.append(f'"{col_spc.col}" = ?')
                params.append(special)
            where = " AND ".join(conditions)
            df = con.execute(f"""
                SELECT "{col_yea.col}" AS y, "{col_mon.col}" AS m, SUM("{col_amt.col}") AS s
                FROM "{safe}"
                WHERE {where}
                GROUP BY "{col_yea.col}", "{col_mon.col}"
            """, params).df()
            totals.append(df)
        except Exception:
            continue
    if not totals:
        return 0.0
    combined = pd.concat(totals, ignore_index=True)
    if combined.empty:
        return 0.0
    monthly = combined.groupby(["y", "m"])["s"].sum()
    return round(float(monthly.sum() / months), 2)


# ── Forecast: Engine ──────────────────────────────────────────────────────────
# Expandiert wiederkehrende + einmalige Buchungen zu einer Tagesreihe und
# berechnet kumulative Salden ab den aktuellen Konto-Salden.

def _interval_dates(start: pd.Timestamp, end, itype: str, inum: int,
                    range_start: pd.Timestamp, range_end: pd.Timestamp) -> list:
    """Generiert alle Buchungstermine eines Eintrags innerhalb des Zeitfensters."""
    if   itype == "täglich":         step = pd.DateOffset(days=inum)
    elif itype == "wöchentlich":     step = pd.DateOffset(weeks=inum)
    elif itype == "monatlich":       step = pd.DateOffset(months=inum)
    elif itype == "quartalsweise":   step = pd.DateOffset(months=3 * inum)
    elif itype == "halbjährlich":    step = pd.DateOffset(months=6 * inum)
    elif itype == "jährlich":        step = pd.DateOffset(years=inum)
    else: return []

    dates   = []
    current = start
    safety  = 0   # Hartes Limit gegen Endlosschleifen
    while current <= range_end and (end is None or current <= end):
        if current >= range_start:
            dates.append(current)
        current = current + step
        safety += 1
        if safety > 10000:
            break
    return dates


def _current_balances(ibans: list) -> dict:
    """Liest den aktuellen (letzten) Saldo jedes Kontos aus der DB."""
    balances = {}
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            balances[iban] = 0.0
            continue
        try:
            row = con.execute(
                f'SELECT "{col_sld.col}" FROM "{safe}" '
                f'WHERE "{col_sld.col}" IS NOT NULL '
                f'ORDER BY "{col_dat.col}" DESC, "{col_rid.col}" DESC LIMIT 1'
            ).fetchone()
            balances[iban] = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            balances[iban] = 0.0
    return balances


def compute_forecast(
    horizon_months: int = 24,
    overrides: dict | None = None,
    oneoff_overrides: dict | None = None,
    excluded_ids: set | None = None,
    excluded_oneoff_ids: set | None = None,
    pct_increase: float = 0.0,
    confidence: float = 1.0,
    inflation_map: dict | None = None,
    include_oneoff: bool = True,
    only_active: bool = True,
    forecast_start: date | None = None,
) -> dict:
    """
    Berechnet die Prognose (konsolidiert über alle in den Forecast-Einträgen
    vorkommenden IBANs).
    Parameter:
      overrides:           {forecast_id: alternativer_betrag} (What-If wiederkehrend)
      oneoff_overrides:    {oneoff_id: alternativer_betrag}   (What-If einmalig)
      excluded_ids:        Menge von forecast_ids, die im What-If deaktiviert wurden
      excluded_oneoff_ids: Menge von oneoff_ids, die im What-If deaktiviert wurden
      pct_increase:   globaler Aufschlag in % auf alle Beträge
      confidence:     Multiplikator der Variabilität für Konfidenzband
      inflation_map:  {group: annual_pct} – jährliche Steigerung pro Gruppe
      include_oneoff: einmalige Ereignisse einbeziehen
      only_active:    nur Einträge mit status='aktiv'
      forecast_start: optionaler Startzeitpunkt (1. des Monats); Standard = heute
    Liefert:
      events:         DataFrame aller expandierten Buchungen
      monthly:        DataFrame mit monatlichen Aggregaten + Saldo + Bändern
      balances_start: dict[iban, start_saldo]
    """
    ensure_forecast_tables()
    overrides           = overrides           or {}
    oneoff_overrides    = oneoff_overrides    or {}
    excluded_ids        = excluded_ids        or set()
    excluded_oneoff_ids = excluded_oneoff_ids or set()
    inflation_map       = inflation_map       or {}

    today = pd.Timestamp(date.today()).normalize()
    if forecast_start is not None:
        range_start = pd.Timestamp(forecast_start).normalize().replace(day=1)
    else:
        range_start = today.replace(day=1)
    range_end = range_start + pd.DateOffset(months=horizon_months)

    rec = load_recurring(active_only=only_active)
    one = load_oneoff() if include_oneoff else pd.DataFrame()

    events = []
    for _, r in rec.iterrows():
        if int(r["forecast_id"]) in excluded_ids:
            continue
        base_amount  = overrides.get(int(r["forecast_id"]), float(r["amount"]))
        base_amount *= (1.0 + pct_increase / 100.0)
        infl_pct = float(inflation_map.get(r["group"], 0.0))
        var_pct  = float(r["variability"] or 0)

        st_dt = pd.Timestamp(r["start_date"])
        en_dt = pd.Timestamp(r["end_date"]) if pd.notna(r["end_date"]) else None
        dates = _interval_dates(st_dt, en_dt, r["interval_type"], int(r["interval_num"]),
                                range_start, range_end)
        for d in dates:
            # Inflation: jährliche Steigerung wirkt ab heute
            years_elapsed = max(0.0, (d - today).days / 365.25)
            infl_factor   = (1.0 + infl_pct / 100.0) ** years_elapsed
            amount = base_amount * infl_factor
            spread = abs(amount) * var_pct / 100.0 * confidence
            # lower = ungünstiger Fall (Ausgabe größer / Einnahme kleiner)
            # upper = günstiger Fall  (Ausgabe kleiner / Einnahme größer)
            lower, upper = amount - spread, amount + spread
            events.append({
                "date":      d,
                "amount":    amount,
                "lower":     lower,
                "upper":     upper,
                "applicant": r["applicant"],
                "group":     r["group"] or "Sonstiges",
                "category":  r["category"] or "Sonstiges",
                "relation":  r["relation"] or "–",
                "context":   r["context"] or "–",
                "iban":      r["iban"],
                "source":    "wiederkehrend",
            })

    if include_oneoff and not one.empty:
        for _, o in one.iterrows():
            if int(o["oneoff_id"]) in excluded_oneoff_ids:
                continue
            d = pd.Timestamp(o["event_date"])
            if range_start <= d <= range_end:
                base = oneoff_overrides.get(int(o["oneoff_id"]), float(o["amount"]))
                amt = base * (1.0 + pct_increase / 100.0)
                events.append({
                    "date":      d,
                    "amount":    amt,
                    "lower":     amt,
                    "upper":     amt,
                    "applicant": o["applicant"],
                    "group":     o["group"] or "Sonstiges",
                    "category":  o["category"] or "Sonstiges",
                    "relation":  "–",
                    "context":   "–",
                    "iban":      o["iban"],
                    "source":    "einmalig",
                })

    events_df = pd.DataFrame(events)

    used_ibans = sorted({
        i for i in (rec["iban"].tolist() + (one["iban"].tolist() if not one.empty else []))
        if isinstance(i, str) and i
    })
    balances_start = _current_balances(used_ibans)
    start_total    = sum(balances_start.values())

    # Vollständige Monatsskala
    all_months = pd.date_range(range_start.replace(day=1), range_end, freq="MS")
    skel = pd.DataFrame({"year_month": [d.strftime("%Y-%m") for d in all_months]})

    if events_df.empty:
        monthly = skel.copy()
        monthly["income"] = 0.0
        monthly["expense"] = 0.0
        monthly["net"] = 0.0
        monthly["net_lower"] = 0.0
        monthly["net_upper"] = 0.0
    else:
        events_df["year_month"] = events_df["date"].dt.strftime("%Y-%m")
        agg = events_df.groupby("year_month").agg(
            income    = ("amount", lambda s: s[s > 0].sum()),
            expense   = ("amount", lambda s: s[s < 0].sum()),
            net       = ("amount", "sum"),
            net_lower = ("lower",  "sum"),
            net_upper = ("upper",  "sum"),
        ).reset_index()
        monthly = skel.merge(agg, on="year_month", how="left").fillna(0)

    monthly["saldo"]       = start_total + monthly["net"].cumsum()
    monthly["saldo_lower"] = start_total + monthly["net_lower"].cumsum()
    monthly["saldo_upper"] = start_total + monthly["net_upper"].cumsum()

    return {"events": events_df, "monthly": monthly, "balances_start": balances_start}


def liquidity_warnings(monthly: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Filtert Monate aus Prognose, in denen der untere Saldo unter Schwelle fällt."""
    if monthly.empty:
        return monthly
    return monthly[monthly["saldo_lower"] < threshold].copy()


def forecast_vs_actual(months_back: int = 6, exclude_special: bool = False) -> pd.DataFrame:
    """
    Vergleicht die heute bekannte Prognose-Konfiguration rückwirkend mit den
    tatsächlichen Buchungen der letzten N Monate.
    exclude_special: wenn True, werden Ist-Buchungen mit special=True herausgefiltert.
    """
    today    = pd.Timestamp(date.today()).normalize().replace(day=1)
    win_start = today - pd.DateOffset(months=months_back)

    # Was hätte das aktuelle Forecast-Setup vorhergesagt?
    rec = load_recurring(active_only=True)
    pred_events = []
    for _, r in rec.iterrows():
        st_dt = pd.Timestamp(r["start_date"])
        # Für den Rückblick wird das Muster rückwirkend projiziert — start_date
        # ignorieren wenn es nach dem Fenster-Anfang liegt.
        eff_start = min(st_dt, win_start)
        en_dt = pd.Timestamp(r["end_date"]) if pd.notna(r["end_date"]) else None
        dates = _interval_dates(eff_start, en_dt, r["interval_type"], int(r["interval_num"]),
                                win_start, today)
        for d in dates:
            pred_events.append({"date": d, "amount": float(r["amount"])})
    pred = pd.DataFrame(pred_events)
    if not pred.empty:
        pred["year_month"] = pred["date"].dt.strftime("%Y-%m")
        pred_m = pred.groupby("year_month")["amount"].sum().reset_index(name="prognose")
    else:
        pred_m = pd.DataFrame(columns=["year_month", "prognose"])

    # Tatsächliche Buchungen aus allen Konten
    ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()
    actual_frames = []
    special_clause = f' AND ("{col_spc.col}" IS NULL OR "{col_spc.col}" = FALSE)' if exclude_special else ""
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            continue
        try:
            df = con.execute(f"""
                SELECT "{col_dat.col}" AS date, "{col_amt.col}" AS amount
                FROM "{safe}"
                WHERE "{col_dat.col}" >= ? AND "{col_dat.col}" < ?{special_clause}
            """, [str(win_start.date()), str(today.date())]).df()
            actual_frames.append(df)
        except Exception:
            continue
    if actual_frames:
        actual = pd.concat(actual_frames, ignore_index=True)
        actual["date"] = pd.to_datetime(actual["date"])
        actual["year_month"] = actual["date"].dt.strftime("%Y-%m")
        actual_m = actual.groupby("year_month")["amount"].sum().reset_index(name="ist")
    else:
        actual_m = pd.DataFrame(columns=["year_month", "ist"])

    merged = pred_m.merge(actual_m, on="year_month", how="outer").fillna(0)
    merged["abweichung"] = merged["ist"] - merged["prognose"]
    return merged.sort_values("year_month").reset_index(drop=True)


# ── Geteilte Filter-Hilfsfunktionen & Konstanten ─────────────────────────────

MONTH_NAMES: dict[int, str] = {
    1: "Jan", 2: "Feb", 3: "Mär", 4: "Apr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Dez",
}


def inc_filter(series: pd.Series, sel: list) -> pd.Series:
    """Include-Filter: leer → keine Einschränkung, sonst isin."""
    return series.isin(sel) if sel else pd.Series(True, index=series.index)


def exc_filter(series: pd.Series, sel: list) -> pd.Series:
    """Exclude-Filter: leer → keine Einschränkung, sonst Ausschluss."""
    return ~series.isin(sel) if sel else pd.Series(True, index=series.index)


# ── Sidebar-Navigation ────────────────────────────────────────────────────────

def navigation() -> None:
    """Rendert die Seitenleisten-Navigation und den Sperren-Button."""
    with st.sidebar:
        st.title("MyFin :euro:")

        st.markdown("<br>" * 3, unsafe_allow_html=True)
        st.divider()
        st.markdown("<br>" * 3, unsafe_allow_html=True)

        st.page_link("app_dashboard.py", label="📊 Analysieren")
        st.page_link("app_assign.py",    label="🔖 Zuordnen")
        st.page_link("app_forecast.py",  label="🔮 Vorhersagen")
        st.page_link("app_retrieve.py",  label="🏦 Importieren")
        st.page_link("app_admin.py",     label="⚙️ Administrieren")

        st.markdown("<br>" * 3, unsafe_allow_html=True)
        st.divider()
        st.markdown("<br>" * 3, unsafe_allow_html=True)

        if st.button("🔒 Sperren", width="stretch"):
            logout()
