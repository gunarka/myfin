"""
app_functions.py
Gemeinsame Hilfsfunktionen, Konfiguration und Datenbankverbindung.
Enthält: Spalten-Definitionen, Keyring-Verwaltung, DB-Verbindung, Navigation.
"""

import json
import os
import re
import shutil
import signal
import time
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dataclasses import dataclass, field
from datetime import date
import logging
from keyrings.cryptfile.cryptfile import CryptFileKeyring
import keyring
from pathlib import Path
import duckdb

from db_schema import (
    ensure_core_tables,
    create_transaction_table,
    migrate_transaction_tables,
    get_or_create_category_id as _db_get_or_create_category_id,
    get_or_create_group_id as _db_get_or_create_group_id,
)

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
            log.debug("Konnte IBAN-Whitelist nicht laden.", exc_info=True)
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
    Transaktionstabellen (IBANs) werden über die Lese-View "{iban}_v"
    angesprochen, die category_id gegen die normalisierten groups/categories-
    Tabellen zu Klartext "group"/"category" auflöst.
    """
    col_names = ", ".join(f'"{c}"' for c in cols)
    # Interne Tabellen (Großbuchstaben, kein IBAN-Muster) direkt durchlassen
    if re.fullmatch(r"[A-Za-z_]+", table):
        safe = table   # z.B. "Accounts" – keine IBAN, kein User-Input
    else:
        safe = safe_table_name(table) + "_v"
    return f'SELECT {col_names} FROM "{safe}"'


def get_config(cols: list) -> dict:
    """Gibt column_config für die übergebenen Spalten zurück."""
    return {c.col: c.typ(label=c.lab, **c.cfg) for c in cols}


def query_all_accounts(select_sql: str, params: list | None = None) -> pd.DataFrame:
    """
    Führt eine SELECT-Query über alle bekannten Konto-Tabellen aus und hängt
    die Ergebnisse zusammen (Spalte 'iban' wird ergänzt).
    `select_sql` muss "{t}" als Platzhalter für den validierten Tabellennamen
    enthalten, z. B. 'SELECT amount FROM "{t}" WHERE amount > ?'.

    Führt EINE UNION-ALL-Query über alle Konten aus statt einer Query pro
    Konto (Performance bei vielen Konten). `{t}` referenziert dabei die
    Lese-View "{iban}_v" (group/category bereits aus der Normalisierung
    aufgelöst), nicht die Rohtabelle. Setzt voraus, dass alle Transaktions-
    tabellen dasselbe Spalten-Set haben – wird durch migrate_transaction_tables()
    sichergestellt. Schlägt die kombinierte Query dennoch fehl (z.B.
    inkonsistentes Schema), wird ein leeres DataFrame zurückgegeben statt
    die App abstürzen zu lassen.
    """
    ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()
    parts = []
    for iban in ibans:
        try:
            safe = safe_table_name(iban)
        except ValueError:
            continue
        parts.append(
            f"SELECT *, '{safe}' AS iban FROM ({select_sql.format(t=safe + '_v')}) t_{len(parts)}"
        )
    if not parts:
        return pd.DataFrame()
    try:
        return con.execute(" UNION ALL ".join(parts), (params or []) * len(parts)).df()
    except Exception:
        log.debug("UNION-ALL-Query über alle Konten fehlgeschlagen.", exc_info=True)
        return pd.DataFrame()


# ── Design-System (gemeinsam für alle Seiten mit Plotly-Charts) ──────────────

DASHBOARD_COLORS: dict[str, str] = {
    "bg":      "#0D0F14",
    "surface": "#161920",
    "border":  "#252830",
    "text":    "#E8EAF0",
    "muted":   "#6B7280",
    "green":   "#00E5A0",
    "red":     "#FF4D6A",
    "blue":    "#4D9FFF",
    "amber":   "#FFB547",
    "purple":  "#A78BFA",
}


def make_plotly_theme(colors: dict = DASHBOARD_COLORS) -> dict:
    """Gibt das gemeinsame Plotly-Layout-Theme zurück (dark, transparent)."""
    return dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="'Noto Sans', monospace", color=colors["text"], size=11),
        margin=dict(l=8, r=8, t=36, b=8),
        colorway=[colors["blue"], colors["green"], colors["amber"], colors["purple"], colors["red"],
                  "#38BDF8", "#F472B6", "#34D399"],
    )


# ── IBAN-Extraktion aus zusammengeführten Empfängerfeldern ───────────────────
# Workaround für Banken (u.a. DKB), bei denen python-fints applicant_iban leer
# lässt und die IBAN stattdessen im applicant_name-Feld belässt – teils sogar
# ohne jedes Trennzeichen zum Firmennamen (z.B. "DE75...0LOGPAY GMBH").
# Ein reiner Greedy-Regex-Match kann das Ende der IBAN in solchen Fällen nicht
# erkennen, daher wird die genormte Länge je Ländercode nachgeschlagen.

# Offizielle IBAN-Längen je Länderpräfix (IBAN Registry, Stand 2024).
_IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16,
    "BG": 22, "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24,
    "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18,
    "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23,
    "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32,
    "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25,
    "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24,
    "XK": 20,
}


def _iban_checksum_valid(iban: str) -> bool:
    """Prüft eine IBAN per Mod-97-Verfahren (ISO 7064)."""
    iban = iban.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def extract_embedded_iban(name: str | None) -> tuple[str | None, str]:
    """
    Sucht in einem Namensfeld nach einer eingebetteten, gültigen IBAN und
    trennt sie ab – auch ohne Trennzeichen zwischen IBAN und Name.
    Nur ein Fallback für Fälle mit leerem applicant_iban: die Ländercode-Länge
    (IBAN Registry) plus Mod-97-Prüfsumme verhindern, dass Namensbestandteile
    fälschlich als IBAN interpretiert werden.
    Gibt (iban_oder_None, bereinigter_name) zurück.
    """
    if not name:
        return None, name or ""

    # Leerzeichen-freie Großschreib-Version aufbauen, dabei Rückverweis auf
    # die Original-Position jedes Zeichens mitführen (für sauberes Entfernen).
    idx_map: list[int] = []
    chars: list[str] = []
    for i, ch in enumerate(name):
        if not ch.isspace():
            chars.append(ch.upper())
            idx_map.append(i)
    candidate = "".join(chars)

    for m in re.finditer(r"[A-Z]{2}\d{2}", candidate):
        start  = m.start()
        cc     = candidate[start:start + 2]
        length = _IBAN_LENGTHS.get(cc)
        if not length or start + length > len(candidate):
            continue
        iban_candidate = candidate[start:start + length]
        if not _iban_checksum_valid(iban_candidate):
            continue

        orig_start = idx_map[start]
        orig_end   = idx_map[start + length - 1] + 1
        cleaned = name[:orig_start] + name[orig_end:]
        cleaned = re.sub(r"^[\s,;/-]+|[\s,;/-]+$", "", cleaned)
        return iban_candidate, cleaned or name

    return None, name


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
# col_catid: physische FK-Spalte in Transaktions-/recurring-/oneoff-Tabellen
# (normalisiertes Schema). col_grp/col_cat bleiben als Klartext-Spaltennamen
# gültig, weil die Lese-Views ("{iban}_v", "recurring_v", "oneoff_v")
# category_id zu "group"/"category" auflösen. col_catid wird ausschließlich
# beim Schreiben (INSERT/UPDATE auf die Rohtabelle) verwendet.
col_catid = col("category_id",    "Kategorie-ID")
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

# ── Altersvorsorge-Spalten ─────────────────────────────────────────────────────
col_pid    = col("plan_id",         "ID")
col_pers   = col("person",          "Person",          {"width": "small"})
col_pname  = col("name",            "Name",            {"width": "medium"})
col_ptyp   = col("typ",             "Typ",             {"width": "small"})
col_panb   = col("anbieter",        "Anbieter",        {"width": "medium"})
col_pmon   = col("monatl_beitrag",  "Beitrag/Mon.",    {"width": "small"})
col_pwert  = col("aktueller_wert",  "Aktueller Wert",  {"width": "small"})
col_prente = col("erwartete_rente", "Erw. Rente/Mon.", {"width": "small"})
col_prend  = col("rendite_pct",     "Rendite p.a.",    {"width": "small"})
col_pbeg   = col("rentenbeginn",    "Rentenbeginn",    {"width": "small", "format": "DD.MM.YYYY"}, st.column_config.DateColumn)

# ── Gesetzliche Rente: Jahres-Entgelte ────────────────────────────────────────
col_giid = col("income_id",             "ID")
col_gjah = col("jahr",                  "Jahr",              {"width": "small"})
col_gind = col("individuelles_entgelt", "Ihr Entgelt (€)",   {"width": "small"})
col_gdur = col("durchschnittsentgelt",  "Ø-Entgelt D (€)",   {"width": "small"})
col_gep  = col("entgeltpunkte",         "Entgeltpunkte",     {"width": "small"})

INTERVAL_TYPES = ["täglich", "wöchentlich", "monatlich", "quartalsweise", "halbjährlich", "jährlich"]
STATUS_TYPES   = ["aktiv", "pausiert", "beendet"]
PENSION_TYPES  = ["gesetzlich", "betrieblich", "Riester", "Rürup", "ETF-Sparplan", "Immobilie", "Sonstige"]
RENTENFAKTOR_OPTIONS: dict[str, float] = {
    "Altersrente (1,0)":                    1.0,
    "Volle Erwerbsminderungsrente (1,0)":   1.0,
    "Teilw. Erwerbsminderungsrente (0,5)":  0.5,
    "Große Witwen-/Witwerrente (0,55)":     0.55,
    "Kleine Witwen-/Witwerrente (0,25)":    0.25,
    "Halbwaisenrente (0,1)":                0.1,
    "Vollwaisenrente (0,2)":                0.2,
}

COL_LABELS: dict[str, str] = {c.col: c.lab for c in [
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app, col_anm,
    col_loc, col_dat, col_da1, col_da2, col_mon, col_yea, col_inf,
    col_add, col_brf, col_eer, col_ibn, col_new, col_rid, col_sld, col_spc,
    col_fid, col_iban, col_int_typ, col_int_num,
    col_st_dat, col_en_dat, col_status, col_var_pct, col_note,
    col_oid, col_oo_dat,
    col_pid, col_pers, col_pname, col_ptyp, col_panb, col_pmon, col_pwert,
    col_prente, col_prend, col_pbeg,
    col_giid, col_gjah, col_gind, col_gdur, col_gep,
]}


# ── Fingerprint für Duplikatprüfung (Import) ──────────────────────────────────
# bank_reference + prozessiertes Datum (col_dat, NICHT das rohe Bank-Datum,
# da dieses nicht immer eindeutig ist) + applicant_iban + Betrag (Cent) + purpose.
# WICHTIG: FINGERPRINT_SQL (DB-seitig, für Abgleich gegen bereits gespeicherte
# Buchungen) und build_fingerprint() (DataFrame-seitig, für frisch geladene
# Buchungen) müssen exakt dieselbe Reihenfolge/Trennzeichen verwenden.

FINGERPRINT_SQL = (
    f'COALESCE("{col_brf.col}", \'\') || chr(31) || '
    f'"{col_dat.col}"::DATE::VARCHAR || chr(31) || '
    f'COALESCE("{col_ibn.col}", \'\') || chr(31) || '
    f'CAST(ROUND("{col_amt.col}" * 100) AS BIGINT)::VARCHAR || chr(31) || '
    f'COALESCE("{col_inf.col}", \'\')'
)


def build_fingerprint(df: pd.DataFrame) -> pd.Series:
    """
    Erzeugt den Duplikat-Fingerprint für einen DataFrame frisch geladener
    Buchungen (FinTS-Download oder CSV-Import). Muss exakt zu FINGERPRINT_SQL
    passen. Nutzt das bereits prozessierte Datum (col_dat), nicht das rohe
    Bank-/Wertstellungsdatum, da dieses allein nicht immer eindeutig ist.
    """
    return (
        df[col_brf.col].fillna("").astype(str) + "\x1f"
        + pd.to_datetime(df[col_dat.col]).dt.strftime("%Y-%m-%d") + "\x1f"
        + df[col_ibn.col].fillna("").astype(str) + "\x1f"
        + (df[col_amt.col] * 100).round().astype("int64").astype(str) + "\x1f"
        + df[col_inf.col].fillna("").astype(str)
    )


# ── Gemeinsame Filter-UI (Dashboard & Zuordnen) ───────────────────────────────

def render_category_filters(
    cat_df: pd.DataFrame,
    all_grps: list[str],
    all_cats: list[str],
    ctxs: list[str],
    rels: list[str],
    search_cols: list[str],
    def_flr_rel=None,
    def_exc_ctx=None,
    def_flr_new=None,
    def_exc_spc=None,
    render_extra=None,
) -> dict:
    """
    Rendert die 'Filter'- und 'Ausschließen'-Expander, die in
    app_dashboard.py und app_assign.py identisch benötigt werden.
    Gruppe und Kategorie schränken sich gegenseitig ein (über
    st.session_state der vorherigen Auswahl, wie im Original-Verhalten).
    `render_extra`, falls angegeben, wird als erstes im 'Filter'-Expander
    aufgerufen (z.B. für den seitenspezifischen Zeitraum-Slider) und sein
    Rückgabewert unter dem Key "extra" im Ergebnis-dict zurückgegeben.
    """
    with st.expander("🔍 Filter"):
        extra_result = render_extra() if render_extra is not None else None
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            _sel_cat = st.session_state.get("flr_cat", [])
            _grp_opts = sorted(
                cat_df[cat_df[col_cat.col].isin(_sel_cat)][col_grp.col].unique().tolist(), key=str.lower
            ) if _sel_cat else all_grps
            flr_grp = st.multiselect(col_grp.lab, _grp_opts, key="flr_grp")
            _sel_grp = st.session_state.get("flr_grp", [])
            _cat_opts = sorted(
                cat_df[cat_df[col_grp.col].isin(_sel_grp)][col_cat.col].unique().tolist(), key=str.lower
            ) if _sel_grp else all_cats
            flr_cat = st.multiselect(col_cat.lab, _cat_opts, key="flr_cat")
            flr_new = st.multiselect(col_new.lab, [True, False], default=def_flr_new, key="flr_new")
        with c2:
            flr_cnt = st.multiselect(col_ctx.lab, ctxs, key="flr_cnt")
            flr_rel = st.multiselect(col_rel.lab, rels, default=def_flr_rel, key="flr_rel")
            flr_spc = st.multiselect(col_spc.lab, [True, False], key="flr_spc")
        with c3:
            src_col1 = st.selectbox("Suchfeld 1", search_cols, key="src_col1")
            src_txt1 = st.text_input("Text 1", key="src_txt1")
        with c4:
            src_col2 = st.selectbox("Suchfeld 2", search_cols, key="src_col2")
            src_txt2 = st.text_input("Text 2", key="src_txt2")

    with st.expander("🚫 Ausschließen"):
        c1, c2 = st.columns(2)
        with c1:
            _exc_cat_sel = st.session_state.get("exc_cat", [])
            _exc_grp_opts = sorted(
                cat_df[cat_df[col_cat.col].isin(_exc_cat_sel)][col_grp.col].unique().tolist(), key=str.lower
            ) if _exc_cat_sel else all_grps
            exc_grp = st.multiselect(col_grp.lab, _exc_grp_opts, key="exc_grp")
            _exc_grp_sel = st.session_state.get("exc_grp", [])
            _exc_cat_opts = sorted(
                cat_df[cat_df[col_grp.col].isin(_exc_grp_sel)][col_cat.col].unique().tolist(), key=str.lower
            ) if _exc_grp_sel else all_cats
            exc_cat = st.multiselect(col_cat.lab, _exc_cat_opts, key="exc_cat")
            exc_new = st.multiselect(col_new.lab, [True, False], key="exc_new")
        with c2:
            exc_cnt = st.multiselect(col_ctx.lab, ctxs, default=def_exc_ctx, key="exc_cnt")
            exc_rel = st.multiselect(col_rel.lab, rels, key="exc_rel")
            exc_spc = st.multiselect(col_spc.lab, [True, False], default=def_exc_spc, key="exc_spc")

    return dict(
        flr_grp=flr_grp, exc_grp=exc_grp,
        flr_cat=flr_cat, exc_cat=exc_cat,
        flr_new=flr_new, exc_new=exc_new,
        flr_cnt=flr_cnt, exc_cnt=exc_cnt,
        flr_rel=flr_rel, exc_rel=exc_rel,
        flr_spc=flr_spc, exc_spc=exc_spc,
        src_col1=src_col1, src_txt1=src_txt1,
        src_col2=src_col2, src_txt2=src_txt2,
        extra=extra_result,
    )


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
    Bei der Ersteinrichtung (noch keine Keyring-Datei vorhanden) wird eine
    Passwort-Wiederholung verlangt, um Tippfehler beim einmaligen Festlegen
    des Master-Passworts zu vermeiden.
    """
    if "master_password" not in st.session_state:
        first_setup = not KEYRING_PATH.exists()
        st.title("🔐 Master-Passwort festlegen" if first_setup else "🔐 Entsperren")
        with st.form("unlock_form"):
            pw  = st.text_input("Master-Passwort", type="password")
            pw2 = (
                st.text_input("Master-Passwort wiederholen", type="password")
                if first_setup else None
            )
            submitted = st.form_submit_button(
                "Festlegen" if first_setup else "Entsperren", width="stretch"
            )

        if submitted:
            if not pw:
                st.error("Bitte Passwort eingeben.")
            elif first_setup and pw != pw2:
                st.error("❌ Passwörter stimmen nicht überein.")
            elif first_setup and len(pw) < 8:
                st.error("❌ Bitte mindestens 8 Zeichen verwenden.")
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
    """
    Beendet die Anwendung: schließt die DB-Verbindung sauber, ersetzt den
    Seiteninhalt im Browser durch einen neutralen Abschluss-Bildschirm ohne
    Finanzdaten und terminiert danach den Streamlit-Server-Prozess.
    SICHERHEIT: Ein automatisches Schließen des Browser-Tabs ist technisch
    nicht zuverlässig möglich – Browser verbieten window.close(), sobald ein
    Tab mehr als einen History-Eintrag hat, was bei Streamlits
    Seiten-Navigation (Analysieren/Zuordnen/…) praktisch immer der Fall ist.
    Stattdessen wird sichergestellt, dass keine sensiblen Daten mehr sichtbar
    oder im DOM vorhanden sind; die DB-Verbindung wird vorher sauber
    geschlossen (vermeidet Locks auf der lokalen Datei), das Master-Passwort
    verbleibt nur im Prozessspeicher und verschwindet mit dessen Beendigung.
    """
    log.info("Anwendung wird geschlossen.")
    con_obj = st.session_state.get("con")
    if con_obj is not None:
        try:
            con_obj.close()
        except Exception:
            log.debug("DB-Verbindung konnte beim Schließen nicht sauber geschlossen werden.", exc_info=True)

    _show_shutdown_screen()
    time.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)


def _show_shutdown_screen() -> None:
    """
    Ersetzt den kompletten Seiteninhalt im Browser (Eltern-Dokument) durch
    einen neutralen Abschluss-Bildschirm ohne Finanzdaten und versucht
    zusätzlich, best effort, den Tab automatisch zu schließen (funktioniert
    nur falls der Tab seit dem Start nie zwischen Seiten gewechselt hat).
    SICHERHEIT: Entfernt alle zuvor gerenderten Kontodaten/Beträge aus dem
    DOM, damit nach dem Schließen nichts Sensibles im Tab sichtbar bleibt.
    """
    components.html(
        """
        <script>
        (function() {
            const doc = window.parent.document;
            doc.body.innerHTML = `
                <div style="
                    position:fixed; inset:0; display:flex; flex-direction:column;
                    align-items:center; justify-content:center; gap:1rem;
                    background:#0e1117; color:#fafafa;
                    font-family:'Source Sans Pro', sans-serif; text-align:center;
                    padding:2rem; z-index:999999;
                ">
                    <div style="font-size:3rem;">🔒</div>
                    <div style="font-size:1.4rem; font-weight:600;">MyFin wurde beendet</div>
                    <div style="opacity:0.75; max-width:28rem;">
                        Server-Verbindung und Datenbank wurden geschlossen.
                        Bitte schließen Sie diesen Tab jetzt manuell
                        (Strg+W bzw. Cmd+W).
                    </div>
                </div>
            `;
            try { window.top.open('', '_self'); window.top.close(); } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


# ── Datenbankverbindung ───────────────────────────────────────────────────────
# Verbindung wird einmalig pro Session erstellt und wiederverwendet.
# SICHERHEIT: Nur lokale Datei, kein Netzwerk-Endpoint.

def _connect_duckdb_with_recovery(db_path: Path) -> duckdb.DuckDBPyConnection:
    """
    Verbindet mit der DuckDB-Datei; fängt ein defektes WAL-Log ab (z.B. nach
    hartem Prozessabbruch/Absturz – bekannter interner DuckDB-Fehler beim
    WAL-Replay, siehe duckdb#19712). Ohne Behandlung crasht die App dauerhaft
    beim Start, da die WAL-Datei bei jedem Verbindungsversuch erneut fehlschlägt.

    Wiederherstellung: DB-Datei + WAL werden zuerst unverändert nach
    .data/backups/ kopiert (nichts wird destruktiv gelöscht), danach wird die
    defekte WAL entfernt und erneut verbunden. Ergebnis ist der Stand des
    letzten Checkpoints; nur seit dem Absturz nicht gecheckpointete Änderungen
    können fehlen. Ein Hinweis dazu wird im session_state hinterlegt und einmalig
    in der UI angezeigt.
    """
    try:
        return duckdb.connect(str(db_path))
    except duckdb.Error:
        wal_path = db_path.with_name(db_path.name + ".wal")
        if not wal_path.exists():
            raise  # anderes Problem – nicht durch WAL-Entfernung behebbar

        log.error("DuckDB-WAL defekt – versuche Wiederherstellung ab letztem Checkpoint.", exc_info=True)
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(wal_path, backup_dir / f"{wal_path.name}.{stamp}.bak")
        if db_path.exists():
            shutil.copy2(db_path, backup_dir / f"{db_path.name}.{stamp}.bak")
        wal_path.unlink()

        st.session_state["wal_recovery_notice"] = (
            "⚠️ Beim letzten Start wurde ein beschädigtes Datenbank-Log erkannt "
            f"(z.B. durch harten Programmabbruch). Es wurde nach `.data/backups/` "
            f"gesichert (Zeitstempel {stamp}) und die Datenbank auf den letzten "
            "Checkpoint zurückgesetzt. Seit dem Absturz nicht gespeicherte "
            "Änderungen können fehlen – bitte betroffene Importe/Zuordnungen prüfen."
        )
        try:
            return duckdb.connect(str(db_path))
        except duckdb.Error:
            log.critical("Wiederherstellung fehlgeschlagen – Datenbank bleibt unlesbar.", exc_info=True)
            raise


if "con" not in st.session_state:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state.con = _connect_duckdb_with_recovery(DB_PATH)

con: duckdb.DuckDBPyConnection = st.session_state.con

# Guard: Verbindung in schlechtem Zustand (z.B. pending query) → neu verbinden
try:
    con.execute("SELECT 1")
except Exception:
    st.session_state.con = _connect_duckdb_with_recovery(DB_PATH)
    con = st.session_state.con

# Kern-Tabellen sicherstellen (relevant bei Erststart nach git-Clone)
try:
    ensure_core_tables(con)
except Exception:
    log.warning("Konnte Kern-Tabellen nicht anlegen/prüfen.", exc_info=True)

# Migration: Spaltenabgleich aller IBAN-Transaktionstabellen gegen das
# zentrale Schema (db_schema.TRANSACTION_COLUMNS) – generischer Ersatz für
# die vorherige, auf einzelne Spalten (z.B. nur "note") hartkodierte Migration.
# Wird nur einmalig pro Session ausgeführt (ADD COLUMN IF NOT EXISTS ist
# idempotent, aber das spart unnötige Queries bei jedem Rerun).
if not st.session_state.get("_migration_done"):
    try:
        migrate_transaction_tables(con)
    except Exception:
        log.warning("Migration (Spaltenabgleich) fehlgeschlagen.", exc_info=True)
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


# ── Kategorien (normalisiert: groups + categories) ────────────────────────────

def ensure_categories_table() -> None:
    """
    Legt 'groups'/'categories' normalisiert an, falls noch nicht vorhanden
    (inkl. automatischer Migration eines alten, kombinierten Schemas).
    Nutzt das zentrale Schema aus db_schema.py.
    """
    ensure_core_tables(con)


def get_or_create_category_id(group: str | None, category: str | None) -> int | None:
    """
    Löst eine Gruppe/Kategorie-Textkombination in ihre category_id auf und
    legt Gruppe/Kategorie bei Bedarf neu an (normalisiertes Schema).
    Gibt None zurück, wenn Gruppe oder Kategorie leer/None sind.
    Session-Cache für Kategorien wird bei Neuanlage invalidiert.
    """
    ensure_categories_table()
    cid = _db_get_or_create_category_id(con, group, category)
    if cid is not None:
        st.session_state.pop("categories_cache", None)
    return cid


def load_categories() -> pd.DataFrame:
    """
    Lädt alle Gruppen/Kategorien aus den normalisierten Tabellen groups/categories.
    Gibt einen DataFrame mit Spalten 'group' und 'category' zurück (Klartext,
    wie vor der Normalisierung), sortiert nach group, category.
    Legt die Tabellen automatisch an wenn sie noch nicht existieren.
    Ergebnis wird pro Session gecacht; Invalidierung über save_category()/
    get_or_create_category_id().
    """
    if "categories_cache" not in st.session_state:
        ensure_categories_table()
        st.session_state["categories_cache"] = con.execute("""
            SELECT g."name" AS "group", c."name" AS "category"
            FROM "categories" c
            JOIN "groups" g ON c."group_id" = g."group_id"
            ORDER BY "group", "category"
        """).df().fillna("")
    return st.session_state["categories_cache"]


def save_category(group: str, category: str) -> bool:
    """
    Fügt eine neue Gruppe/Kategorie-Kombination in die normalisierten
    Tabellen groups/categories ein.
    Gibt True zurück wenn neu angelegt, False wenn bereits vorhanden.
    Beide Felder müssen nicht-leer sein.
    """
    if not group.strip() or not category.strip():
        raise ValueError("Gruppe und Kategorie dürfen nicht leer sein.")
    ensure_categories_table()
    existing = con.execute(
        """SELECT c."category_id" FROM "categories" c
           JOIN "groups" g ON c."group_id" = g."group_id"
           WHERE g."name" = ? AND c."name" = ?""",
        [group.strip(), category.strip()],
    ).fetchone()
    if existing:
        return False
    _db_get_or_create_category_id(con, group, category)
    st.session_state.pop("categories_cache", None)
    return True


# ── Account-Liste ─────────────────────────────────────────────────────────────

def list_saved_users():
    """
    Gibt alle Konten aus der Accounts-Tabelle als DataFrame zurück.
    KEY  = interner Schlüssel mit _ (Keyring-kompatibel, nicht ändern)
    LABEL = Anzeigename mit · für die UI
    Ergebnis wird pro Session gecacht (Schlüssel 'saved_users_cache').
    Invalidierung: st.session_state.pop("saved_users_cache", None) nach
    Konto-Änderungen (Hinzufügen/Entfernen in app_admin.py).
    """
    if "saved_users_cache" not in st.session_state:
        accounts = con.sql(
            build_select(["Person", "Bank", "Konto", "IBAN", "Abruf"], "Accounts")
        ).df()
        accounts["KEY"]   = accounts["Person"] + "_" + accounts["Bank"] + "_" + accounts["Konto"]
        accounts["LABEL"] = accounts["Person"] + " · " + accounts["Bank"] + " · " + accounts["Konto"]
        st.session_state["saved_users_cache"] = accounts
    return st.session_state["saved_users_cache"]


# ── Forecast: Tabellen-Setup ──────────────────────────────────────────────────

def ensure_forecast_tables() -> None:
    """
    Legt alle Tabellen für die Prognosefunktion an, falls noch nicht vorhanden.
    Schemas:
      - recurring:  wiederkehrende Buchungen
      - oneoff:     einmalige geplante Ereignisse
      - scenarios:  benannte What-If-Szenarien (JSON mit Parametern)
      - inflation:  jährlicher %-Aufschlag pro Gruppe
    Nutzt das zentrale Schema aus db_schema.py.
    """
    ensure_core_tables(con)


# ── Forecast: CRUD wiederkehrende Buchungen ───────────────────────────────────
# Externe Schnittstelle (Dict-Keys "group"/"category") bleibt Text, wie vor
# der Normalisierung – intern wird gegen category_id übersetzt (recurring_v
# löst beim Lesen category_id wieder zu Klartext auf).

def load_recurring(active_only: bool = False) -> pd.DataFrame:
    ensure_forecast_tables()
    where = "WHERE status = 'aktiv'" if active_only else ""
    return con.execute(f'SELECT * FROM recurring_v {where} ORDER BY "group", "category", applicant').df()


def save_recurring(rec: dict) -> int:
    """Fügt einen wiederkehrenden Eintrag ein und liefert die neue ID."""
    ensure_forecast_tables()
    rec = dict(rec)
    rec["category_id"] = get_or_create_category_id(rec.get("group"), rec.get("category"))
    cols = ["applicant", "amount", "category_id", "relation", "context",
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
    """Konvertiert numpy-Skalare in native Python-Typen für DuckDB; NaN/NaT -> NULL
    (sonst würde z.B. ein NaN in einer berechneten Spalte als Float statt als
    SQL-NULL geschrieben und AVG()/IS NOT NULL-Filter unbrauchbar machen)."""
    if hasattr(v, "item"):
        v = v.item()
    return None if pd.isna(v) else v


_RECURRING_COLS = frozenset([
    "applicant", "amount", "category_id", "relation", "context",
    "iban", "interval_type", "interval_num", "start_date", "end_date",
    "status", "variability", "note",
])


def update_recurring(forecast_id: int, fields: dict) -> None:
    """
    Aktualisiert einzelne Felder eines Eintrags. "group"/"category" (Text,
    kommen z.B. aus einem grp_cat-SelectboxColumn-Edit) werden dabei
    gemeinsam in category_id übersetzt.
    """
    if not fields:
        return
    fields = dict(fields)
    if "group" in fields or "category" in fields:
        g = fields.pop("group", None)
        c = fields.pop("category", None)
        fields["category_id"] = get_or_create_category_id(g, c)
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
    return con.execute('SELECT * FROM oneoff_v ORDER BY event_date').df()


def save_oneoff(ev: dict) -> int:
    ensure_forecast_tables()
    ev = dict(ev)
    ev["category_id"] = get_or_create_category_id(ev.get("group"), ev.get("category"))
    cols = ["applicant", "amount", "category_id", "relation", "context", "iban", "event_date", "note"]
    vals = [_py(ev.get(c)) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_str = ", ".join(f'"{c}"' for c in cols)
    return con.execute(
        f'INSERT INTO oneoff ({col_str}) VALUES ({placeholders}) RETURNING oneoff_id',
        vals,
    ).fetchone()[0]


_ONEOFF_COLS = frozenset([
    "applicant", "amount", "category_id", "relation", "context", "iban", "event_date", "note",
])

def update_oneoff(oneoff_id: int, fields: dict) -> None:
    if not fields:
        return
    fields = dict(fields)
    if "group" in fields or "category" in fields:
        g = fields.pop("group", None)
        c = fields.pop("category", None)
        fields["category_id"] = get_or_create_category_id(g, c)
    unknown = set(fields.keys()) - _ONEOFF_COLS
    if unknown:
        raise ValueError(f"Unbekannte Spalten für oneoff: {unknown}")
    set_clause = ", ".join(f'"{k}" = ?' for k in fields.keys())
    con.execute(
        f'UPDATE oneoff SET {set_clause} WHERE oneoff_id = ?',
        [_py(v) for v in fields.values()] + [int(oneoff_id)],
    )


def delete_oneoff(oneoff_id: int) -> None:
    con.execute('DELETE FROM oneoff WHERE oneoff_id = ?', [oneoff_id])


# ── Forecast: Inflation pro Gruppe ────────────────────────────────────────────

def load_inflation() -> pd.DataFrame:
    ensure_forecast_tables()
    return con.execute('SELECT * FROM inflation_v ORDER BY "group"').df()


def upsert_inflation(group: str, annual_pct: float) -> None:
    ensure_forecast_tables()
    gid = _db_get_or_create_group_id(con, group)
    con.execute(
        'INSERT OR REPLACE INTO inflation ("group_id", "annual_pct") VALUES (?, ?)',
        [gid, annual_pct],
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


# ── Altersvorsorge: CRUD Vorsorge-Bausteine ───────────────────────────────────
# Bewusst als eigenständige Tabelle ohne group/category-FK: Vorsorge-Verträge
# sind kein Buchungs-, sondern ein Bestandsobjekt (Kapitalwert + Beitrag).
# Optionale Verknüpfung zur Prognose erfolgt separat über einen `recurring`-
# Eintrag mit group="Altersvorsorge", falls der Beitrag im Cashflow auftauchen soll.

def ensure_pension_tables() -> None:
    ensure_core_tables(con)
    _migrate_pension_scenarios_to_global()


def load_pension_plans(active_only: bool = False) -> pd.DataFrame:
    ensure_pension_tables()
    where = "WHERE status = 'aktiv'" if active_only else ""
    return con.execute(f'SELECT * FROM pension_plans {where} ORDER BY typ, name, anbieter').df()


_PENSION_COLS = [
    "person", "name", "typ", "anbieter", "monatl_beitrag", "aktueller_wert",
    "erwartete_rente", "rendite_pct", "rentenbeginn", "status", "note",
]


def save_pension_plan(plan: dict) -> int:
    ensure_pension_tables()
    vals = [_py(plan.get(c)) for c in _PENSION_COLS]
    placeholders = ", ".join(["?"] * len(_PENSION_COLS))
    col_str = ", ".join(f'"{c}"' for c in _PENSION_COLS)
    return con.execute(
        f'INSERT INTO pension_plans ({col_str}) VALUES ({placeholders}) RETURNING plan_id',
        vals,
    ).fetchone()[0]


def update_pension_plan(plan_id: int, fields: dict) -> None:
    if not fields:
        return
    unknown = set(fields.keys()) - set(_PENSION_COLS)
    if unknown:
        raise ValueError(f"Unbekannte Spalten für pension_plans: {unknown}")
    set_clause = ", ".join(f'"{k}" = ?' for k in fields.keys())
    con.execute(
        f'UPDATE pension_plans SET {set_clause} WHERE plan_id = ?',
        [_py(v) for v in fields.values()] + [int(plan_id)],
    )


def delete_pension_plan(plan_id: int) -> None:
    con.execute('DELETE FROM pension_plans WHERE plan_id = ?', [plan_id])


# ── Gesetzliche Rente: CRUD Jahres-Entgelte ───────────────────────────────────
# Eigene Tabelle je Person/Jahr (individuelles Entgelt + zugehöriges statist.
# Durchschnittsentgelt), Basis für die Entgeltpunkte-Berechnung. Rein lokal,
# keine externen Abrufe.

_PENSION_INCOME_COLS = ["person", "jahr", "individuelles_entgelt", "durchschnittsentgelt"]


def load_pension_income(person: str) -> pd.DataFrame:
    ensure_pension_tables()
    return con.execute(
        'SELECT * FROM pension_income WHERE person = ? ORDER BY jahr', [person]
    ).df()


def save_pension_income_year(person: str, jahr: int, individuelles_entgelt: float,
                              durchschnittsentgelt: float) -> int:
    """Legt einen Jahres-Entgelt-Eintrag an oder aktualisiert ihn (Upsert über
    Person+Jahr, das fachlich eindeutig sein muss)."""
    ensure_pension_tables()
    existing = con.execute(
        'SELECT income_id FROM pension_income WHERE person = ? AND jahr = ?',
        [person, int(jahr)],
    ).fetchone()
    if existing:
        con.execute(
            'UPDATE pension_income SET individuelles_entgelt = ?, durchschnittsentgelt = ? '
            'WHERE income_id = ?',
            [_py(individuelles_entgelt), _py(durchschnittsentgelt), existing[0]],
        )
        return existing[0]
    return con.execute(
        'INSERT INTO pension_income (person, jahr, individuelles_entgelt, durchschnittsentgelt) '
        'VALUES (?, ?, ?, ?) RETURNING income_id',
        [person, int(jahr), _py(individuelles_entgelt), _py(durchschnittsentgelt)],
    ).fetchone()[0]


def update_pension_income(income_id: int, fields: dict) -> None:
    if not fields:
        return
    unknown = set(fields.keys()) - set(_PENSION_INCOME_COLS)
    if unknown:
        raise ValueError(f"Unbekannte Spalten für pension_income: {unknown}")
    set_clause = ", ".join(f'"{k}" = ?' for k in fields.keys())
    con.execute(
        f'UPDATE pension_income SET {set_clause} WHERE income_id = ?',
        [_py(v) for v in fields.values()] + [int(income_id)],
    )


def delete_pension_income(income_id: int) -> None:
    con.execute('DELETE FROM pension_income WHERE income_id = ?', [int(income_id)])


INCOME_START_YEAR = 1995  # Jahresentgelte-Tabelle wird ab diesem Jahr vorausgefüllt


def ensure_pension_income_years(person: str, start_year: int, end_year: int) -> None:
    """Stellt sicher, dass für `person` im Bereich [start_year, end_year] für jedes
    Jahr ein pension_income-Eintrag existiert. Fehlende Jahre werden mit
    individuellem Entgelt 0 € und dem statistischen Durchschnittsentgelt
    vorbelegt (Anlage 1 SGB VI); bestehende Einträge bleiben unverändert."""
    ensure_pension_tables()
    existing_years = {
        int(r[0]) for r in con.execute(
            'SELECT jahr FROM pension_income WHERE person = ?', [person]
        ).fetchall()
    }
    for jahr in range(start_year, end_year + 1):
        if jahr not in existing_years:
            con.execute(
                'INSERT INTO pension_income (person, jahr, individuelles_entgelt, durchschnittsentgelt) '
                'VALUES (?, ?, ?, ?)',
                [person, jahr, 0.0, durchschnittsentgelt_fuer_jahr(jahr)],
            )


# ── Gesetzliche Rente: Referenzdaten & Berechnung ─────────────────────────────
# Reine lokale Berechnung nach dem gesetzlichen Grundschema (§§ 63ff. SGB VI):
# Rente = Summe Entgeltpunkte × Zugangsfaktor × Rentenartfaktor × aktueller
# Rentenwert. Keine externen Abrufe – Durchschnittsentgelt-Referenztabelle und
# Rentenwert sind statische, im Code hinterlegte Werte (Anlage 1 SGB VI bzw.
# manuelle Eingabe), die der Nutzer bei Bedarf überschreiben kann.

# Durchschnittsentgelt aller Versicherten je Kalenderjahr (Anlage 1 SGB VI).
# Werte ab 2002 in Euro (amtlich); Jahre davor aus DM zum festen Kurs
# 1 EUR = 1,95583 DM umgerechnet. Dient nur als editierbare Vorbelegung.
DURCHSCHNITTSENTGELT_REF: dict[int, float] = {
    1951: 1830, 1952: 1969, 1953: 2076, 1954: 2165, 1955: 2325, 1956: 2477,
    1957: 2578, 1958: 2725, 1959: 2864, 1960: 3119, 1961: 3437, 1962: 3747,
    1963: 3975, 1964: 4329, 1965: 4719, 1966: 5043, 1967: 5225, 1968: 5543,
    1969: 6053, 1970: 6822, 1971: 7634, 1972: 8352, 1973: 9354, 1974: 10421,
    1975: 11150, 1976: 11931, 1977: 12754, 1978: 13417, 1979: 14155, 1980: 15075,
    1981: 15799, 1982: 16463, 1983: 17022, 1984: 17533, 1985: 18041, 1986: 18727,
    1987: 19289, 1988: 19887, 1989: 20484, 1990: 21447, 1991: 22712, 1992: 23939,
    1993: 24633, 1994: 25126, 1995: 25905, 1996: 26423, 1997: 26660, 1998: 27060,
    1999: 27358, 2000: 27741, 2001: 28231, 2002: 28626, 2003: 28938, 2004: 29060,
    2005: 29202, 2006: 29494, 2007: 29951, 2008: 30625, 2009: 30506, 2010: 31144,
    2011: 32100, 2012: 33002, 2013: 33659, 2014: 34514, 2015: 35363, 2016: 36187,
    2017: 37077, 2018: 38212, 2019: 39301, 2020: 39167, 2021: 40463, 2022: 42053,
    2023: 44732, 2024: 47085, 2025: 50493, 2026: 51944,
}

AKTUELLER_RENTENWERT = 42.52  # € je Entgeltpunkt (West/Gesamt), Stand 01.07.2026


def durchschnittsentgelt_fuer_jahr(jahr: int) -> float:
    """Statistisches Durchschnittsentgelt für ein Jahr (Anlage 1 SGB VI);
    außerhalb des hinterlegten Bereichs wird der nächstgelegene bekannte
    Jahreswert verwendet (Näherung für sehr frühe/zukünftige Jahre)."""
    if jahr in DURCHSCHNITTSENTGELT_REF:
        return float(DURCHSCHNITTSENTGELT_REF[jahr])
    nearest = min(DURCHSCHNITTSENTGELT_REF, key=lambda y: abs(y - jahr))
    return float(DURCHSCHNITTSENTGELT_REF[nearest])


def entgeltpunkte_jahr(individuelles_entgelt: float, durchschnittsentgelt: float) -> float:
    """Entgeltpunkte eines Jahres = individuelles Entgelt / Durchschnittsentgelt."""
    if not durchschnittsentgelt:
        return 0.0
    return individuelles_entgelt / durchschnittsentgelt


def _add_months(d: date, months: int) -> date:
    """Datum + N Monate (kann negativ sein), immer auf den 1. des Zielmonats."""
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    return date(y, m + 1, 1)


def regelaltersgrenze_zusatz(geburtsjahr: int) -> tuple[int, int]:
    """Regelaltersgrenze (Jahre, Monate) nach § 235 Abs. 2 SGB VI, gestaffelt
    nach Geburtsjahrgang: 65 (≤1946) → schrittweise 67 (≥1964)."""
    if geburtsjahr <= 1946:
        return 65, 0
    if geburtsjahr <= 1958:
        monate_gesamt = 65 * 12 + (geburtsjahr - 1946)
    elif geburtsjahr <= 1963:
        monate_gesamt = 65 * 12 + 12 + (geburtsjahr - 1958) * 2
    else:
        return 67, 0
    return divmod(monate_gesamt, 12)


def regelaltersrentendatum(geburtsdatum: date) -> date:
    """Datum der Regelaltersgrenze (vereinfacht: 1. des Monats nach Vollendung
    des maßgeblichen Lebensalters)."""
    jahre, monate = regelaltersgrenze_zusatz(geburtsdatum.year)
    return _add_months(geburtsdatum, jahre * 12 + monate + 1)


def zugangsfaktor(monate_abweichung: int) -> float:
    """Zugangsfaktor je Monat Abweichung von der Regelaltersgrenze:
    -0,3 %/Monat bei früherem, +0,5 %/Monat bei späterem Rentenbeginn."""
    satz = 0.003 if monate_abweichung < 0 else 0.005
    return round(1 + monate_abweichung * satz, 4)


def berechne_gesetzliche_rente(
    income_df: pd.DataFrame,
    geburtsdatum: date,
    monate_abweichung: int,
    zukunfts_ep_rate: float,
    rentenwert: float,
    rentenwert_entwicklung_pct: float,
    rentenfaktor: float,
    ep_override: float | None = None,
    erwerbsminderung_start: date | None = None,
) -> dict:
    """Berechnet die gesetzliche Rente aus den Jahres-Entgelten und Annahmen.
    `income_df` benötigt die Spalten jahr/individuelles_entgelt/durchschnittsentgelt.
    `ep_override`: falls gesetzt (z. B. Wert aus der amtlichen Renteninformation),
    ersetzt dieser Wert die aus `income_df` summierten bisherigen Entgeltpunkte.
    `erwerbsminderung_start`: falls gesetzt, wird dieses Datum direkt als
    Rentenbeginn verwendet (statt Regelaltersgrenze ± `monate_abweichung`) – für
    Erwerbsminderungsrenten mit festem Startdatum; der Zugangsfaktor wird
    weiterhin über den Monatsabstand des Startdatums zur Regelaltersgrenze
    ermittelt. Rückgabe: dict mit allen Zwischen- und Endwerten (siehe Schlüssel
    unten)."""
    today = date.today()
    df = income_df.copy()
    if df.empty:
        df["ep"] = pd.Series(dtype=float)
    else:
        df["ep"] = df.apply(
            lambda r: entgeltpunkte_jahr(r["individuelles_entgelt"], r["durchschnittsentgelt"]),
            axis=1,
        )
    summe_ep_bisher = float(df["ep"].sum()) if not df.empty else 0.0
    ep_bisher_quelle = "historie"
    if ep_override is not None:
        summe_ep_bisher = float(ep_override)
        ep_bisher_quelle = "renteninformation"
    letzte5 = df.sort_values("jahr", ascending=False).head(5) if not df.empty else df
    ep_letzte5_avg = float(letzte5["ep"].mean()) if not letzte5.empty else 0.0
    letztes_jahr = int(df["jahr"].max()) if not df.empty else today.year - 1

    regelaltersgrenze_datum = regelaltersrentendatum(geburtsdatum)
    if erwerbsminderung_start is not None:
        rentenbeginn = erwerbsminderung_start
        monate_abweichung_effektiv = (
            (rentenbeginn.year - regelaltersgrenze_datum.year) * 12
            + (rentenbeginn.month - regelaltersgrenze_datum.month)
        )
    else:
        rentenbeginn = _add_months(regelaltersgrenze_datum, monate_abweichung)
        monate_abweichung_effektiv = monate_abweichung
    zf = zugangsfaktor(monate_abweichung_effektiv)

    zukunftsjahre = max(0.0, (rentenbeginn - date(letztes_jahr, 12, 31)).days / 365.25)
    zukuenftige_ep = zukunfts_ep_rate * zukunftsjahre
    gesamt_ep = summe_ep_bisher + zukuenftige_ep

    jahre_bis_rentenbeginn = max(0.0, (rentenbeginn - today).days / 365.25)
    rentenwert_bei_beginn = rentenwert * (1 + rentenwert_entwicklung_pct / 100) ** jahre_bis_rentenbeginn

    rente_monatlich = gesamt_ep * zf * rentenfaktor * rentenwert_bei_beginn

    return {
        "entgeltpunkte_df": df,
        "summe_ep_bisher": summe_ep_bisher,
        "ep_bisher_quelle": ep_bisher_quelle,
        "ep_letzte5_avg": ep_letzte5_avg,
        "letztes_jahr": letztes_jahr,
        "regelaltersgrenze_datum": regelaltersgrenze_datum,
        "rentenbeginn": rentenbeginn,
        "zugangsfaktor": zf,
        "zukunftsjahre": zukunftsjahre,
        "zukuenftige_ep": zukuenftige_ep,
        "gesamt_ep": gesamt_ep,
        "rentenwert_bei_beginn": rentenwert_bei_beginn,
        "rente_monatlich": rente_monatlich,
        "rente_jaehrlich": rente_monatlich * 12,
    }


# ── Gesetzliche Rente: Netto-Berechnung (vereinfachte Einkommensteuer) ───────
# Schätzt die Netto-Rente im ersten vollen Rentenjahr: Besteuerungsanteil nach
# Rentenbeginn-Jahrgang (§ 22 Nr. 1 Satz 3 Buchst. a EStG, inkl. Wachstums-
# chancengesetz 2024), Werbungskosten-/Sonderausgaben-Pauschbetrag, Grund-
# freibetrag (mit Entwicklungs-Annahme), Einkommensteuer-Grundtarif (§ 32a
# EStG), Solidaritätszuschlag (§§ 3, 4 SolzG, mit Freigrenze/Milderungszone)
# sowie Kranken-/Pflegeversicherung für KVdR-Pflichtversicherte (§§ 249a, 250
# SGB V, § 59 SGB XI). Betriebsrenten/Riester/Rürup u. a. Vorsorge-Bausteine
# der Person werden als weitere, voll steuerpflichtige/-beitragspflichtige
# Alterseinkünfte einbezogen. Ohne Kirchensteuer und ohne Zusammenveranlagung
# (dafür ggf. den Parameter `sonstige_abzuege` nutzen) – vereinfachtes Modell,
# ersetzt keine Steuer-/Sozialversicherungsberatung. Rein lokale Berechnung,
# keine externen Abrufe.

# Steuerpflichtiger Anteil der Rente je Renteneintrittsjahr (%): 50 % bis 2005,
# +2 %-Punkte/Jahr bis 2020 (80 %), +1 %-Punkt/Jahr 2021–2022 (82 %), ab 2023
# +0,5 %-Punkte/Jahr bis 100 % im Jahr 2058 (Wachstumschancengesetz 03/2024).
def besteuerungsanteil_pct(rentenbeginn_jahr: int) -> float:
    jahr = min(max(int(rentenbeginn_jahr), 2005), 2058)
    if jahr <= 2020:
        return 50.0 + (jahr - 2005) * 2.0
    if jahr <= 2022:
        return 80.0 + (jahr - 2020) * 1.0
    return 82.0 + (jahr - 2022) * 0.5


RENTE_WERBUNGSKOSTEN_PAUSCHBETRAG = 102.0  # €/Jahr, § 9a Satz 1 Nr. 3 EStG
SONDERAUSGABEN_PAUSCHBETRAG = 36.0         # €/Jahr, Einzelveranlagung, § 10c EStG

# Grundfreibetrag (Einzelveranlagung) laut Sozialversicherungs-/Existenzminimum-
# Berichten; amtlich für 2022–2026, danach Fortschreibung mit Entwicklungs-Annahme.
GRUNDFREIBETRAG_REF: dict[int, float] = {
    2022: 10347, 2023: 10908, 2024: 11784, 2025: 12096, 2026: 12348,
}


def grundfreibetrag_fuer_jahr(jahr: int, entwicklung_pct: float) -> float:
    """Grundfreibetrag für ein Jahr: amtlicher Wert falls bekannt, sonst mit
    `entwicklung_pct` p.a. ab dem letzten hinterlegten Jahr fortgeschrieben
    (bzw. auf den ältesten bekannten Wert geklemmt für Jahre davor)."""
    jahr = int(jahr)
    if jahr in GRUNDFREIBETRAG_REF:
        return float(GRUNDFREIBETRAG_REF[jahr])
    letztes_jahr = max(GRUNDFREIBETRAG_REF)
    erstes_jahr = min(GRUNDFREIBETRAG_REF)
    if jahr > letztes_jahr:
        return GRUNDFREIBETRAG_REF[letztes_jahr] * (1 + entwicklung_pct / 100) ** (jahr - letztes_jahr)
    return float(GRUNDFREIBETRAG_REF[erstes_jahr])


# Referenz-Formeltarif (Grundtabelle, Einzelveranlagung) nach § 32a EStG,
# Veranlagungszeitraum 2025 (letzter vollständig amtlich dokumentierter Tarif).
_EST_REFERENZ_GRUNDFREIBETRAG = 12096.0


def einkommensteuer_grundtarif_2025(zve: float) -> float:
    """Tarifliche Einkommensteuer (Grundtabelle) nach § 32a EStG, VZ 2025."""
    x = max(0.0, zve)
    if x <= 12096:
        return 0.0
    if x <= 17443:
        y = (x - 12096) / 10000
        return (932.30 * y + 1400) * y
    if x <= 68480:
        z = (x - 17443) / 10000
        return (176.64 * z + 2397) * z + 1015.13
    if x <= 277825:
        return 0.42 * x - 10911.92
    return 0.45 * x - 19246.67


def einkommensteuer(zve: float, grundfreibetrag: float) -> float:
    """Schätzt die tarifliche Einkommensteuer für ein beliebiges Jahr: der
    2025er-Referenztarif wird proportional zum Verhältnis von `grundfreibetrag`
    zum Referenz-Grundfreibetrag (12.096 €) gestreckt – eine vereinfachte
    Fortschreibung der Tarif-Eckwerte, analog zum jährlichen Ausgleich der
    „kalten Progression“. Für das Referenzjahr 2025 entspricht dies exakt dem
    amtlichen Tarif."""
    scale = max(grundfreibetrag / _EST_REFERENZ_GRUNDFREIBETRAG, 0.01) if grundfreibetrag else 1.0
    return scale * einkommensteuer_grundtarif_2025(zve / scale)


# Solidaritätszuschlag (§§ 3, 4 SolzG): 5,5 % der Einkommensteuer, aber nur
# oberhalb einer Freigrenze; direkt darüber Milderungszone (max. 11,9 % des
# Differenzbetrags), die einen Belastungssprung vermeidet.
SOLI_SATZ = 0.055
SOLI_MILDERUNGSSATZ = 0.119

# Freigrenze (Einzelveranlagung) laut SolzG/Steuerfortentwicklungsgesetz;
# amtlich für 2022–2026, danach Fortschreibung mit Grundfreibetrag-Annahme.
SOLI_FREIGRENZE_REF: dict[int, float] = {
    2022: 16956, 2023: 17543, 2024: 18130, 2025: 19950, 2026: 20350,
}


def soli_freigrenze_fuer_jahr(jahr: int, entwicklung_pct: float) -> float:
    """Soli-Freigrenze (Einzelveranlagung) für ein Jahr, analog zu
    `grundfreibetrag_fuer_jahr` fortgeschrieben (beide Werte werden vom
    Gesetzgeber in der Praxis etwa parallel zur kalten Progression angepasst)."""
    jahr = int(jahr)
    if jahr in SOLI_FREIGRENZE_REF:
        return float(SOLI_FREIGRENZE_REF[jahr])
    letztes_jahr = max(SOLI_FREIGRENZE_REF)
    erstes_jahr = min(SOLI_FREIGRENZE_REF)
    if jahr > letztes_jahr:
        return SOLI_FREIGRENZE_REF[letztes_jahr] * (1 + entwicklung_pct / 100) ** (jahr - letztes_jahr)
    return float(SOLI_FREIGRENZE_REF[erstes_jahr])


def solidaritaetszuschlag(einkommensteuer_jahr: float, freigrenze: float) -> float:
    """Soli auf die Einkommensteuer eines Jahres, inkl. Milderungszone."""
    if einkommensteuer_jahr <= freigrenze:
        return 0.0
    return min(SOLI_SATZ * einkommensteuer_jahr, SOLI_MILDERUNGSSATZ * (einkommensteuer_jahr - freigrenze))


# Kranken-/Pflegeversicherung für KVdR-Pflichtversicherte (Stand 2026).
# Gesetzliche Rente: Beitrag wird hälftig von Rentenversicherung und Rentner
# getragen (§ 249a SGB V) – beim Krankenversicherungsbeitrag, nicht bei der
# Pflegeversicherung (§ 59 Abs. 1 SGB XI: dort trägt der Rentner den vollen
# Satz allein). Betriebsrenten/Riester/Rürup ("Versorgungsbezüge") unterliegen
# oberhalb eines Freibetrags dem vollen (ungeteilten) Beitragssatz (§ 250
# Abs. 1 Nr. 1 SGB V).
KV_ALLGEMEINER_BEITRAGSSATZ = 14.6       # %, bundeseinheitlich
KV_ZUSATZBEITRAG_DURCHSCHNITT = 2.9      # %, GKV-Schätzerkreis 2026 (kassenindividuell abweichend)
PV_BEITRAGSSATZ = 3.6                    # %, mit Kind, seit 01.01.2025
PV_KINDERLOS_ZUSCHLAG = 0.6              # %-Punkte Zuschlag für Kinderlose ab 23 Jahren
KV_PV_BEITRAGSBEMESSUNGSGRENZE_JAHR = 69750.0  # €/Jahr, 2026 (einheitlich West/Ost)
BETRIEBSRENTEN_FREIBETRAG_KV_JAHR = 2373.0     # €/Jahr ≈ 1/20 Bezugsgröße West, 2026


def berechne_netto_rente(
    rente_brutto_jahr: float,
    rentenbeginn_jahr: int,
    weitere_alterseinkuenfte_jahr: float,
    grundfreibetrag_entwicklung_pct: float,
    sonstige_abzuege: float = 0.0,
    kvdr_pflichtversichert: bool = True,
    kv_zusatzbeitrag_pct: float = KV_ZUSATZBEITRAG_DURCHSCHNITT,
    pv_kinderlos: bool = False,
) -> dict:
    """Schätzt die Netto-Jahres-/Monatsrente im ersten vollen Rentenjahr.
    `weitere_alterseinkuenfte_jahr`: sonstige, voll steuer-/beitragspflichtige
    Alterseinkünfte p.a. (z. B. Betriebsrente, Riester, Rürup) – erhöhen das
    zu versteuernde Einkommen und werden bei der Nettoquote der gesetzlichen
    Rente anteilig berücksichtigt. `sonstige_abzuege`: zusätzliche, frei
    definierbare Abzüge (z. B. private Zusatzversicherung). `kvdr_pflicht-
    versichert`: KV/PV-Beiträge nur berechnen, wenn als gesetzlich kranken-
    versicherter Rentner (KVdR) zutreffend."""
    besteuerungsanteil = besteuerungsanteil_pct(rentenbeginn_jahr)
    steuerpflichtiger_anteil = rente_brutto_jahr * besteuerungsanteil / 100
    rentenfreibetrag_eur = rente_brutto_jahr - steuerpflichtiger_anteil

    # ── Kranken-/Pflegeversicherung ───────────────────────────────────────
    kv_beitrag_rente = kv_beitrag_weitere = pv_beitrag_rente = pv_beitrag_weitere = 0.0
    if kvdr_pflichtversichert:
        basis_gesamt = rente_brutto_jahr + weitere_alterseinkuenfte_jahr
        kappungsfaktor = (KV_PV_BEITRAGSBEMESSUNGSGRENZE_JAHR / basis_gesamt
                           if basis_gesamt > KV_PV_BEITRAGSBEMESSUNGSGRENZE_JAHR else 1.0)
        rente_basis = rente_brutto_jahr * kappungsfaktor
        weitere_basis = max(0.0, weitere_alterseinkuenfte_jahr * kappungsfaktor - BETRIEBSRENTEN_FREIBETRAG_KV_JAHR)

        kv_eigenanteil_pct = (KV_ALLGEMEINER_BEITRAGSSATZ + kv_zusatzbeitrag_pct) / 2
        kv_voll_pct = KV_ALLGEMEINER_BEITRAGSSATZ + kv_zusatzbeitrag_pct
        pv_pct = PV_BEITRAGSSATZ + (PV_KINDERLOS_ZUSCHLAG if pv_kinderlos else 0.0)

        kv_beitrag_rente = rente_basis * kv_eigenanteil_pct / 100
        pv_beitrag_rente = rente_basis * pv_pct / 100
        kv_beitrag_weitere = weitere_basis * kv_voll_pct / 100
        pv_beitrag_weitere = weitere_basis * pv_pct / 100

    kv_beitrag_jahr = kv_beitrag_rente + kv_beitrag_weitere
    pv_beitrag_jahr = pv_beitrag_rente + pv_beitrag_weitere

    # ── Einkommensteuer ────────────────────────────────────────────────────
    grundfreibetrag = grundfreibetrag_fuer_jahr(rentenbeginn_jahr, grundfreibetrag_entwicklung_pct)
    sonderausgaben_abzug = max(SONDERAUSGABEN_PAUSCHBETRAG, kv_beitrag_jahr + pv_beitrag_jahr) + sonstige_abzuege

    zve = max(0.0, steuerpflichtiger_anteil + weitere_alterseinkuenfte_jahr
              - RENTE_WERBUNGSKOSTEN_PAUSCHBETRAG - sonderausgaben_abzug)
    einkommensteuer_jahr = einkommensteuer(zve, grundfreibetrag)

    # ── Solidaritätszuschlag ───────────────────────────────────────────────
    soli_freigrenze = soli_freigrenze_fuer_jahr(rentenbeginn_jahr, grundfreibetrag_entwicklung_pct)
    soli_jahr = solidaritaetszuschlag(einkommensteuer_jahr, soli_freigrenze)

    # ── Zusammenführung & anteilige Zuordnung zur gesetzlichen Rente ──────
    abgaben_jahr = einkommensteuer_jahr + soli_jahr
    brutto_gesamt_jahr = rente_brutto_jahr + weitere_alterseinkuenfte_jahr
    steuerpfl_gesamt = steuerpflichtiger_anteil + weitere_alterseinkuenfte_jahr
    anteil_gesetzlich = steuerpflichtiger_anteil / steuerpfl_gesamt if steuerpfl_gesamt else 1.0
    abgaben_gesetzliche_rente = abgaben_jahr * anteil_gesetzlich

    netto_gesetzliche_rente_jahr = rente_brutto_jahr - abgaben_gesetzliche_rente - kv_beitrag_rente - pv_beitrag_rente
    netto_gesamt_jahr = brutto_gesamt_jahr - abgaben_jahr - kv_beitrag_jahr - pv_beitrag_jahr

    return {
        "besteuerungsanteil_pct": besteuerungsanteil,
        "steuerpflichtiger_anteil": steuerpflichtiger_anteil,
        "rentenfreibetrag_eur": rentenfreibetrag_eur,
        "grundfreibetrag": grundfreibetrag,
        "zve": zve,
        "einkommensteuer_jahr": einkommensteuer_jahr,
        "soli_jahr": soli_jahr,
        "kv_beitrag_jahr": kv_beitrag_jahr,
        "pv_beitrag_jahr": pv_beitrag_jahr,
        "abgaben_jahr": abgaben_jahr + kv_beitrag_jahr + pv_beitrag_jahr,
        "brutto_gesamt_jahr": brutto_gesamt_jahr,
        "netto_gesamt_jahr": netto_gesamt_jahr,
        "netto_gesamt_monat": netto_gesamt_jahr / 12,
        "netto_gesetzliche_rente_jahr": netto_gesetzliche_rente_jahr,
        "netto_gesetzliche_rente_monat": netto_gesetzliche_rente_jahr / 12,
    }


# ── Gesetzliche Rente: Fakten (je Person) & Szenarien (global) ───────────────
# Fakten (Geburtsdatum, aktueller Rentenwert, ggf. Entgeltpunkte-Override aus
# der Renteninformation) werden je Person, benannte Szenarien (Annahmen zur
# Rentenentwicklung: Renteneintritt/Zugangsfaktor, Rentenfaktor, ggf. festes
# Erwerbsminderungs-Startdatum, Rentenwert-Entwicklung, zukünftige Entgelt-
# punkte) **personenunabhängig** in der vorhandenen `scenarios`-Tabelle
# abgelegt (namensbasiert, kein neues Schema nötig). Welches Szenario für
# welche Person zur Berechnung herangezogen wird, sowie die Netto-Berechnungs-
# Annahmen und die einbezogenen weiteren Vorsorge-Bausteine, werden separat
# je Person gespeichert (siehe unten) – so kann z. B. das Szenario "Basis"
# von mehreren Personen gemeinsam genutzt werden.

def _pension_facts_key(person: str) -> str:
    return f"pension_facts::{person}"


def save_pension_facts(person: str, facts: dict) -> None:
    save_scenario(_pension_facts_key(person), facts)


def load_pension_facts(person: str) -> dict:
    return load_scenario(_pension_facts_key(person)) or {}


def _pension_scenario_key(name: str) -> str:
    return f"pension_scenario::{name}"


def save_pension_scenario(name: str, params: dict) -> None:
    save_scenario(_pension_scenario_key(name), params)


def load_pension_scenario(name: str) -> dict | None:
    return load_scenario(_pension_scenario_key(name))


def list_pension_scenarios() -> list[str]:
    prefix = _pension_scenario_key("")
    return [n[len(prefix):] for n in list_scenarios() if n.startswith(prefix)]


def delete_pension_scenario(name: str) -> None:
    delete_scenario(_pension_scenario_key(name))


# ── Gesetzliche Rente: Auswahl je Person (aktives Szenario, einbezogene
# Bausteine, Netto-Berechnungs-Annahmen) ─────────────────────────────────────
# Getrennt von den (globalen) Szenarien gespeichert, da mehrere Personen
# dasselbe Szenario nutzen, aber unterschiedliche Bausteine/Steuerannahmen
# haben können.

def _pension_active_scenario_key(person: str) -> str:
    return f"pension_active_scenario::{person}"


def set_pension_active_scenario(person: str, name: str | None) -> None:
    """Legt fest, welches (globale) Szenario für `person` zur Berechnung der
    gesetzlichen Rente herangezogen wird. `name=None` entfernt die Auswahl."""
    if name is None:
        delete_scenario(_pension_active_scenario_key(person))
    else:
        save_scenario(_pension_active_scenario_key(person), {"name": name})


def get_pension_active_scenario(person: str) -> str | None:
    """Name des für `person` ausgewählten Szenarios, falls vorhanden, sonst None."""
    data = load_scenario(_pension_active_scenario_key(person))
    return data.get("name") if data else None


def _pension_netto_annahmen_key(person: str) -> str:
    return f"pension_netto_annahmen::{person}"


def save_pension_netto_annahmen(person: str, annahmen: dict) -> None:
    save_scenario(_pension_netto_annahmen_key(person), annahmen)


def load_pension_netto_annahmen(person: str) -> dict:
    return load_scenario(_pension_netto_annahmen_key(person)) or {}


def _pension_baustein_auswahl_key(person: str) -> str:
    return f"pension_baustein_auswahl::{person}"


def save_pension_baustein_auswahl(person: str, plan_ids: list[int]) -> None:
    save_scenario(_pension_baustein_auswahl_key(person), {"plan_ids": [int(p) for p in plan_ids]})


def load_pension_baustein_auswahl(person: str) -> list[int]:
    data = load_scenario(_pension_baustein_auswahl_key(person))
    return list(data.get("plan_ids", [])) if data else []


def _pension_zukunft_ep_key(person: str, scenario_name: str) -> str:
    return f"pension_zukunft_ep::{person}::{scenario_name}"


def save_pension_zukunft_ep(person: str, scenario_name: str, value: float) -> None:
    """„Zukünftige Entgeltpunkte pro Jahr“ ist zugleich eine persönliche Angabe
    (individuelle Erwerbsbiografie) und eine Szenario-Annahme – daher weder im
    globalen Szenario noch rein je Person, sondern je Kombination aus Person
    **und** Szenario gespeichert."""
    save_scenario(_pension_zukunft_ep_key(person, scenario_name), {"value": value})


def load_pension_zukunft_ep(person: str, scenario_name: str) -> float | None:
    data = load_scenario(_pension_zukunft_ep_key(person, scenario_name))
    return float(data["value"]) if data else None


def save_pension_last_context(person: str | None = None, scenario: str | None = None) -> None:
    """Merkt sich die zuletzt bearbeitete Person bzw. das zuletzt erstellte/
    gespeicherte Szenario als Default für die Ergebnis-Ansichten. Teil-Updates
    (nur `person` oder nur `scenario`) überschreiben nur das jeweilige Feld."""
    ctx = load_scenario("pension_last_context") or {}
    if person is not None:
        ctx["person"] = person
    if scenario is not None:
        ctx["scenario"] = scenario
    save_scenario("pension_last_context", ctx)


def load_pension_last_context() -> dict:
    return load_scenario("pension_last_context") or {}


def _migrate_pension_scenarios_to_global() -> None:
    """Einmalige, idempotente Migration in zwei Stufen:

    1) Version 1 → 2: Bis Version 1 waren Szenarien zur gesetzlichen Rente je
       Person gespeichert (Schlüssel "pension_scenario::<Person>::<Name>") und
       enthielten zusätzlich die Netto-Berechnungs-Annahmen sowie „Zukünftige
       Entgeltpunkte pro Jahr". Ab Version 2 sind Rentenentwicklungs-Szenarien
       global (personenunabhängig); Netto-Annahmen, die aktive Szenario-
       Auswahl sowie „Zukünftige Entgeltpunkte pro Jahr" (zugleich persönliche
       Angabe und Szenario-Annahme) liegen separat je Person bzw. je
       Person+Szenario.
    2) Version 2 → 3: In der ersten Version des globalen Formats war „Zukünftige
       Entgeltpunkte pro Jahr" noch im globalen Szenario abgelegt statt je
       Person+Szenario. Wird best-effort an alle Personen übertragen, die das
       Szenario aktuell als aktiv gewählt haben.

    Bestehende Daten werden verlustfrei überführt – bei einem Namenskonflikt
    (gleicher Szenario-Name, unterschiedliche Annahmen bei verschiedenen
    Personen) wird der Name der zweiten und weiteren Personen mit
    „ (Person)“ ergänzt."""
    if load_scenario("pension_migration_v3_done"):
        return
    legacy_prefix = "pension_scenario::"
    netto_keys = ("grundfreibetrag_entwicklung", "sonstige_abzuege",
                  "kvdr_pflichtversichert", "kv_zusatzbeitrag", "pv_kinderlos")

    for full_key in list_scenarios():
        if not full_key.startswith(legacy_prefix):
            continue
        rest = full_key[len(legacy_prefix):]
        params = load_scenario(full_key) or {}

        if "::" in rest:
            # Stufe 1 → 2: ganz altes, personengebundenes Format.
            person, name = rest.split("::", 1)
            status = params.pop("status", "inaktiv")
            netto_annahmen = {k: params.pop(k) for k in netto_keys if k in params}
            zukunft_ep = params.pop("zukunft_ep", None)

            target_name = name
            existing = load_pension_scenario(target_name)
            if existing is not None and existing != params:
                target_name = f"{name} ({person})"
            save_pension_scenario(target_name, params)
            delete_scenario(full_key)

            if zukunft_ep is not None:
                save_pension_zukunft_ep(person, target_name, zukunft_ep)
            if status == "aktiv":
                set_pension_active_scenario(person, target_name)
            if netto_annahmen:
                bestehende = load_pension_netto_annahmen(person)
                bestehende.update(netto_annahmen)
                save_pension_netto_annahmen(person, bestehende)

        elif "zukunft_ep" in params:
            # Stufe 2 → 3: bereits globales Format, „Zukünftige Entgeltpunkte“
            # aber noch im Szenario statt je Person+Szenario gespeichert.
            name = rest
            zukunft_ep = params.pop("zukunft_ep")
            save_pension_scenario(name, params)
            try:
                accounts_df = list_saved_users()
                personen = accounts_df["Person"].unique().tolist() if not accounts_df.empty else []
            except Exception:
                personen = []
            for person in personen:
                if get_pension_active_scenario(person) == name:
                    save_pension_zukunft_ep(person, name, zukunft_ep)

    save_scenario("pension_migration_v3_done", {"done": True})


# ── Altersvorsorge: Kapital-Hochrechnung ──────────────────────────────────────
# Reine lokale Berechnung (Zinseszins-Formel), keine externen Kurs-/Rentendaten.

def project_capital(current: float, monthly: float, years: float, rate_pct: float) -> float:
    """Zinseszins-Hochrechnung: aktueller Wert + monatliche Sparrate über N Jahre."""
    years = max(0.0, years)
    r = rate_pct / 100 / 12
    n = round(years * 12)
    if n <= 0:
        return current
    fv_current = current * (1 + r) ** n
    fv_savings = monthly * (((1 + r) ** n - 1) / r) if r else monthly * n
    return fv_current + fv_savings


def required_monthly_saving(target_fv: float, current: float, years: float, rate_pct: float) -> float:
    """Nötige monatliche Sparrate, um target_fv bei gegebener Rendite/Zeit zu erreichen."""
    years = max(0.0, years)
    r = rate_pct / 100 / 12
    n = max(1, round(years * 12))
    fv_current = current * (1 + r) ** n
    remaining = max(0.0, target_fv - fv_current)
    denom = (((1 + r) ** n - 1) / r) if r else n
    return remaining / denom if denom else 0.0


# ── Historische Inflation je Kategorie (Referenzdaten, z.B. Destatis-VPI) ────
# Reiner lokaler Import per CSV (manueller Download durch den Nutzer, z.B. von
# GENESIS-Online) – kein automatischer externer Abruf. Dient als Orientierung
# für die Steigerungssätze pro Gruppe in der Cashflow-Prognose.
# category_code ist der stabile Schlüssel (z.B. COICOP-Code "CC13-07223"),
# category das Anzeige-Label. group_code/group_label/level bilden optional
# eine Hierarchie ab (z.B. COICOP-Abteilung), damit Unterkategorien ihrer
# übergeordneten Gruppe zugeordnet bleiben.

# Amtliche COICOP-Abteilungen (Destatis VPI, 12 Abteilungen) – stabil, dient
# als Fallback-Label, falls beim Import nur eine Unterkategorie ohne die
# zugehörige Abteilungs-Zeile selbst importiert wurde.
COICOP_DIVISIONS: dict[str, str] = {
    "01": "Nahrungsmittel und alkoholfreie Getränke",
    "02": "Alkoholische Getränke und Tabakwaren",
    "03": "Bekleidung und Schuhe",
    "04": "Wohnung, Wasser, Strom, Gas und andere Brennstoffe",
    "05": "Möbel, Leuchten, Geräte u.a. Haushaltszubehör",
    "06": "Gesundheit",
    "07": "Verkehr",
    "08": "Post und Telekommunikation",
    "09": "Freizeit, Unterhaltung und Kultur",
    "10": "Bildungswesen",
    "11": "Gaststätten- und Beherbergungsdienstleistungen",
    "12": "Andere Waren und Dienstleistungen",
}


def ensure_inflation_history_table() -> None:
    ensure_core_tables(con)


def load_inflation_history(category_codes: list[str] | None = None) -> pd.DataFrame:
    ensure_inflation_history_table()
    if category_codes:
        placeholders = ", ".join(["?"] * len(category_codes))
        return con.execute(
            f'SELECT * FROM inflation_history WHERE category_code IN ({placeholders}) '
            f'ORDER BY category_code, date',
            category_codes,
        ).df()
    return con.execute('SELECT * FROM inflation_history ORDER BY category_code, date').df()


def list_inflation_history_categories() -> pd.DataFrame:
    """Distinct Kategorien mit Gruppierungs-Metadaten (für gruppierte Auswahl in der UI)."""
    ensure_inflation_history_table()
    return con.execute(
        'SELECT DISTINCT category_code, category, group_code, group_label, level '
        'FROM inflation_history ORDER BY group_code NULLS LAST, level NULLS LAST, category'
    ).df()


def delete_inflation_history_category(category_code: str) -> None:
    con.execute('DELETE FROM inflation_history WHERE category_code = ?', [category_code])


def upsert_inflation_history(df: pd.DataFrame) -> int:
    """
    Bulk-Import historischer Referenzdaten. Erwartet mindestens `category`
    und `date` sowie `index_value` und/oder `yoy_pct`. Optional: `category_code`
    (stabiler Schlüssel für wiederholte Importe; Default = category),
    `group_code`/`group_label` (übergeordnete Gruppe, z.B. COICOP-Abteilung)
    und `level` (Gliederungstiefe). Fehlt `yoy_pct`, wird sie aus `index_value`
    berechnet (Veränderung ggü. demselben Datum ein Jahr zuvor, je category_code).
    Gibt die Anzahl importierter/aktualisierter Zeilen zurück.
    """
    ensure_inflation_history_table()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    if "category_code" not in df.columns:
        df["category_code"] = df["category"]
    for c in ("index_value", "yoy_pct", "group_code", "group_label", "level"):
        if c not in df.columns:
            df[c] = None

    if df["index_value"].notna().any():
        idx_lookup = df.set_index(["category_code", "date"])["index_value"]
        date_prev = df["date"] - pd.DateOffset(years=1)

        def _calc_yoy(code, prev_d, current):
            try:
                prev = idx_lookup.loc[(code, prev_d)]
            except KeyError:
                return None
            if pd.isna(prev) or prev == 0:
                return None
            return (current - prev) / prev * 100

        calc = [
            _calc_yoy(code, prev_d, cur)
            for code, prev_d, cur in zip(df["category_code"], date_prev, df["index_value"])
        ]
        df["yoy_pct"] = df["yoy_pct"].where(df["yoy_pct"].notna(), pd.Series(calc, index=df.index))

    df["date"] = df["date"].dt.date
    for _, r in df.iterrows():
        con.execute(
            'INSERT OR REPLACE INTO inflation_history '
            '("category_code", "category", "group_code", "group_label", "level", '
            ' "date", "index_value", "yoy_pct") VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [r["category_code"], r["category"], _py(r.get("group_code")), _py(r.get("group_label")),
             _py(r.get("level")), r["date"], _py(r.get("index_value")), _py(r.get("yoy_pct"))],
        )
    return len(df)


def average_yoy_inflation(category_code: str, years_back: int | None = None) -> float | None:
    """Ø jährliche Veränderung (%) einer Kategorie über die letzten N Jahre (None = gesamter Zeitraum)."""
    ensure_inflation_history_table()
    where = 'WHERE category_code = ? AND yoy_pct IS NOT NULL'
    params = [category_code]
    if years_back:
        cutoff = date.today().replace(year=date.today().year - years_back)
        where += ' AND date >= ?'
        params.append(str(cutoff))
    row = con.execute(f'SELECT AVG(yoy_pct) FROM inflation_history {where}', params).fetchone()
    return float(row[0]) if row and row[0] is not None else None


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
    all_tx = query_all_accounts(
        f"""
        SELECT
            "{col_app.col}"  AS applicant,
            "{col_amt.col}"  AS amount,
            "{col_dat.col}"  AS date,
            "{col_grp.col}"  AS "group",
            "{col_cat.col}"  AS category
        FROM "{{t}}"
        WHERE "{col_dat.col}" >= ?
          AND "{col_app.col}" IS NOT NULL
          AND "{col_amt.col}" IS NOT NULL
        """,
        [str(cutoff.date())],
    )

    if all_tx.empty:
        return pd.DataFrame()

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
    df = query_all_accounts(
        f'SELECT DISTINCT "{field_col}" AS val FROM "{{t}}" WHERE "{field_col}" IS NOT NULL'
    )
    if df.empty:
        return []
    return sorted({str(v) for v in df["val"] if v}, key=str.lower)


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

    conditions = [f'"{col_grp.col}" = ?', f'"{col_dat.col}" >= ?']
    params: list = [group, str(cutoff.date())]
    if category:
        conditions.append(f'"{col_cat.col}" = ?'); params.append(category)
    if relation:
        conditions.append(f'"{col_rel.col}" = ?'); params.append(relation)
    if context:
        conditions.append(f'"{col_ctx.col}" = ?'); params.append(context)
    if special is not None:
        conditions.append(f'"{col_spc.col}" = ?'); params.append(special)
    where = " AND ".join(conditions)

    combined = query_all_accounts(
        f"""
        SELECT "{col_yea.col}" AS y, "{col_mon.col}" AS m, SUM("{col_amt.col}") AS s
        FROM "{{t}}"
        WHERE {where}
        GROUP BY "{col_yea.col}", "{col_mon.col}"
        """,
        params,
    )
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
            log.debug("Saldo-Abfrage fehlgeschlagen für Konto %s", iban, exc_info=True)
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
    special_clause = f' AND ("{col_spc.col}" IS NULL OR "{col_spc.col}" = FALSE)' if exclude_special else ""
    actual = query_all_accounts(
        f"""
        SELECT "{col_dat.col}" AS date, "{col_amt.col}" AS amount
        FROM "{{t}}"
        WHERE "{col_dat.col}" >= ? AND "{col_dat.col}" < ?{special_clause}
        """,
        [str(win_start.date()), str(today.date())],
    )
    if not actual.empty:
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
    """Rendert die Seitenleisten-Navigation und den Schließen-Button."""
    with st.sidebar:
        st.title("MyFin :euro:")

        st.markdown("<br>" * 3, unsafe_allow_html=True)
        st.divider()
        st.markdown("<br>" * 3, unsafe_allow_html=True)

        st.page_link("app_dashboard.py", label="📊 Analysieren")
        st.page_link("app_assign.py",    label="🔖 Zuordnen")
        st.page_link("app_forecast.py",  label="🔮 Vorhersagen")
        st.page_link("app_pension.py",   label="🏖️ Altersvorsorge")
        st.page_link("app_retrieve.py",  label="🏦 Importieren")
        st.page_link("app_admin.py",     label="⚙️ Administrieren")

        st.markdown("<br>" * 3, unsafe_allow_html=True)
        st.divider()
        st.markdown("<br>" * 3, unsafe_allow_html=True)

        if st.button("🔒 Schließen", width="stretch"):
            logout()
