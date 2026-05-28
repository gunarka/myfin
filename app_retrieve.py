"""
app_retrieve.py
Transaktionen per FinTS/HBCI herunterladen oder per CSV importieren.
SICHERHEIT: PIN/Zugangsdaten nur aus Keyring; keine Klartext-Credentials im Code.
            Duplikatprüfung verhindert doppelte Einträge.
"""

from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import io
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING
import time
import pandas as pd
import streamlit as st

if TYPE_CHECKING:
    from fints.client import FinTS3PinTanClient

from app_functions import (
    require_master_password,
    load_fints_credentials,
    list_saved_users,
    safe_table_name,
    get_config,
    con,
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app, col_anm,
    col_loc, col_dat, col_da1, col_da2, col_mon, col_yea,
    col_inf, col_add, col_brf, col_eer, col_ibn,
    col_new, col_rid, col_sld, col_spc,
)

log = logging.getLogger(__name__)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("🏦 Importieren")

# ── Seiteninstanz-Reset ───────────────────────────────────────────────────────
if not st.session_state.get("_retrieve_initialized"):
    st.session_state["_retrieve_initialized"] = True
    for _pfx in ("fints", "csv"):
        st.session_state[f"{_pfx}_downloaded"] = False
        st.session_state.pop(f"{_pfx}_df_prc", None)

# ── Konten laden & nach Abruf-Methode aufteilen ───────────────────────────────
accounts       = list_saved_users()
fints_accounts = accounts[accounts["Abruf"].fillna("FinTS") == "FinTS"].copy()
csv_accounts   = accounts[accounts["Abruf"].fillna("FinTS") == "CSV"].copy()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_fints, tab_csv = st.tabs(["📡 FinTS/HBCI", "📄 CSV-Import"])


# ── Gemeinsame Hilfsfunktionen ────────────────────────────────────────────────

def get_group_category(applicants: list[str]) -> pd.DataFrame:
    """
    Schlägt für bekannte Empfänger die häufigste Gruppe/Kategorie nach.
    Durchsucht ALLE Kontotabellen, um eine möglichst vollständige Zuordnung
    zu erhalten. Leere oder nicht existierende Tabellen werden übersprungen.
    """
    if not applicants:
        return pd.DataFrame(columns=[col_app.col, col_grp.col, col_cat.col])

    con.register("_applicants_tmp", pd.DataFrame({col_app.col: applicants}))
    all_ibans = con.sql('SELECT "IBAN" FROM "Accounts"').df()["IBAN"].tolist()

    union_parts = []
    for iban in all_ibans:
        try:
            safe = safe_table_name(iban)
            union_parts.append(f"""
                SELECT
                    "{col_app.col}",
                    "{col_grp.col}",
                    "{col_cat.col}",
                    COUNT(*) AS cnt
                FROM "{safe}"
                WHERE "{col_app.col}" IN (SELECT "{col_app.col}" FROM _applicants_tmp)
                  AND "{col_grp.col}" IS NOT NULL
                  AND "{col_cat.col}" IS NOT NULL
                GROUP BY "{col_app.col}", "{col_grp.col}", "{col_cat.col}"
            """)
        except ValueError:
            continue

    if not union_parts:
        con.unregister("_applicants_tmp")
        return pd.DataFrame(columns=[col_app.col, col_grp.col, col_cat.col])

    union_sql = " UNION ALL ".join(union_parts)
    try:
        result = con.execute(f"""
            WITH combined AS ({union_sql}),
            ranked AS (
                SELECT
                    "{col_app.col}", "{col_grp.col}", "{col_cat.col}",
                    SUM(cnt) AS total_cnt,
                    ROW_NUMBER() OVER (
                        PARTITION BY "{col_app.col}"
                        ORDER BY SUM(cnt) DESC
                    ) AS rn
                FROM combined
                GROUP BY "{col_app.col}", "{col_grp.col}", "{col_cat.col}"
            )
            SELECT "{col_app.col}", "{col_grp.col}", "{col_cat.col}"
            FROM ranked WHERE rn = 1
        """).df()
    finally:
        con.unregister("_applicants_tmp")

    return result


def db_store(df_prc: pd.DataFrame, var_acc: str, prefix: str) -> None:
    """
    Speichert verarbeitete Transaktionen in der Datenbank.
    Filtert auf Spalten die in der Zieltabelle vorhanden sind – robust für
    FinTS- und CSV-Importe mit unterschiedlichen Spaltenmengen.
    """
    try:
        existing = con.execute(f'SELECT * FROM "{var_acc}" LIMIT 0').df().columns.tolist()
        df_ins   = df_prc[[c for c in df_prc.columns if c in existing]]
        col_str  = ", ".join(f'"{c}"' for c in df_ins.columns.tolist())
        con.register("_df_prc_tmp", df_ins)
        con.execute(f'INSERT INTO "{var_acc}" ({col_str}) SELECT * FROM _df_prc_tmp')
        con.unregister("_df_prc_tmp")
        st.session_state[f"{prefix}_downloaded"] = False
        st.session_state.pop(f"{prefix}_df_prc", None)
        st.success(f"✅ {len(df_prc)} Zeilen erfolgreich gespeichert.")
    except Exception as e:
        log.exception("Fehler beim Speichern in %s", var_acc)
        st.error(f"❌ Fehler beim Speichern: {e}")


def _render_account_buttons(subset: pd.DataFrame, prefix: str) -> str | None:
    """
    Rendert Konto-Auswahl-Buttons für eine gefilterte Kontomenge.
    Gibt den ausgewählten KEY zurück, oder None wenn keine Konten vorhanden.
    """
    all_keys = subset["KEY"].tolist()
    if not all_keys:
        return None

    sel_key = f"{prefix}_sel"
    if sel_key not in st.session_state or st.session_state[sel_key] not in all_keys:
        st.session_state[sel_key] = all_keys[0]

    def _select(k: str) -> None:
        st.session_state[sel_key] = k
        st.session_state[f"{prefix}_downloaded"] = False
        st.session_state.pop(f"{prefix}_df_prc", None)

    _giro  = subset[subset["Konto"].str.lower() == "giro"]
    _other = subset[subset["Konto"].str.lower() != "giro"]
    _fmt   = lambda k: subset.set_index("KEY").loc[k, "LABEL"]

    with st.expander("🏦 Konto", expanded=True):
        c1, c2 = st.columns(2)
        for _col, _grp in [(c1, _giro), (c2, _other)]:
            with _col:
                for _, _row in _grp.iterrows():
                    k = _row["KEY"]
                    st.button(
                        _fmt(k),
                        key=f"btn_{prefix}_{k}",
                        width="stretch",
                        type="primary" if st.session_state[sel_key] == k else "secondary",
                        on_click=_select,
                        args=(k,),
                    )

    return st.session_state[sel_key]


# ── FinTS-Client ──────────────────────────────────────────────────────────────

def _handle_tan(client: FinTS3PinTanClient) -> None:
    """
    Verarbeitet TAN-Anfragen des Banken-Servers.
    Unterstützt Decoupled-Push (z.B. DKB-App-Freigabe).
    """
    from fints.client import NeedTANResponse
    response = client.init_tan_response
    if not isinstance(response, NeedTANResponse):
        return

    if response.decoupled:
        st.info(response.challenge)
        status  = st.empty()
        _poll   = 0
        _max_polls = 100  # 100 × 3s = 5 Minuten
        while isinstance(response, NeedTANResponse) and _poll < _max_polls:
            status.warning(f"⏳ Warte auf App-Freigabe … ({_poll * 3}s)")
            time.sleep(3)
            response = client.send_tan(response, "")
            _poll += 1
        if isinstance(response, NeedTANResponse):
            st.error("⏱️ Timeout: App-Freigabe nicht erhalten (5 min). Bitte erneut versuchen.")
            st.stop()
        status.success("✅ Bestätigt!")
    else:
        st.error(f"Nicht unterstütztes TAN-Verfahren: {response.challenge}")
        st.stop()


def _make_client(creds) -> FinTS3PinTanClient:
    import warnings
    from fints.parser import FinTSParserWarning
    from fints.client import FinTS3PinTanClient
    from fints.utils import minimal_interactive_cli_bootstrap
    warnings.filterwarnings("ignore", category=FinTSParserWarning)
    client = FinTS3PinTanClient(
        bank_identifier=creds.bank_identifier,
        user_id=creds.user_id,
        pin=creds.pin,
        server=creds.server,
        product_id=creds.pid,
    )
    minimal_interactive_cli_bootstrap(client)
    return client


# ── Tab: FinTS/HBCI ───────────────────────────────────────────────────────────

with tab_fints:
    if fints_accounts.empty:
        st.info("Keine FinTS-Konten vorhanden. Konten können unter ⚙️ Administration hinzugefügt werden.")
    else:
        selected = _render_account_buttons(fints_accounts, "fints")
        if selected is None:
            st.stop()

        _acc_row = fints_accounts[fints_accounts["KEY"] == selected].iloc[0]
        try:
            var_acc = safe_table_name(_acc_row["IBAN"])
        except ValueError as e:
            st.error(f"Ungültiges Konto: {e}")
            st.stop()

        _creds_key = f"fints_creds_{selected}"
        if _creds_key not in st.session_state:
            _loaded = load_fints_credentials(selected)
            if _loaded is None:
                st.error("Zugangsdaten konnten nicht geladen werden.")
                st.stop()
            st.session_state[_creds_key] = _loaded
        creds = st.session_state[_creds_key]

        # Datums- & ID-Grenzen + Fingerprint-Set in einer Query
        _meta = con.execute(f"""
            WITH meta AS (
                SELECT
                    MAX("{col_dat.col}"::DATE) AS max_dat,
                    MAX("{col_rid.col}")       AS max_rid,
                    COALESCE(
                        MAX("{col_dat.col}"::DATE) - INTERVAL '10 days',
                        DATE '1970-01-01'
                    )                          AS fp_from
                FROM "{var_acc}"
            )
            SELECT m.max_dat, m.max_rid, fp.fingerprint
            FROM meta m
            LEFT JOIN (
                SELECT
                    COALESCE("{col_brf.col}", '') || chr(31) ||
                    COALESCE("{col_ibn.col}", '') || chr(31) ||
                    COALESCE("{col_inf.col}", '') AS fingerprint,
                    "{col_dat.col}"::DATE         AS dat
                FROM "{var_acc}"
            ) fp ON fp.dat >= m.fp_from
        """).df()

        _row       = _meta.iloc[0]
        start_date = (pd.Timestamp(_row["max_dat"]).date() - timedelta(days=10)) if pd.notna(_row["max_dat"]) else date(1970, 1, 1)
        min_id     = (int(_row["max_rid"]) + 1) if pd.notna(_row["max_rid"]) else 0
        end_date   = date.today()
        tran_ddb   = set(_meta["fingerprint"].dropna())

        if st.button("📥 Transaktionen herunterladen"):
            with st.spinner("Lade Transaktionen …"):
                from fints.client import NeedTANResponse
                client = _make_client(creds)
                with client:
                    _handle_tan(client)

                    bank_accounts = client.get_sepa_accounts()
                    while isinstance(bank_accounts, NeedTANResponse):
                        bank_accounts = client.send_tan(bank_accounts, "")

                    filtered = [a for a in bank_accounts if a.iban.replace(" ", "").upper() == var_acc]
                    if not filtered:
                        st.error(f"IBAN {var_acc} nicht im Konto gefunden.")
                        st.stop()

                    transactions = client.get_transactions(
                        filtered[0], start_date=start_date, end_date=end_date
                    )
                    balance = client.get_balance(filtered[0])

            df_dnl = pd.DataFrame([t.data for t in transactions])

            fingerprint = (
                df_dnl[col_brf.col].fillna("").astype(str)
                + "\x1f"
                + df_dnl[col_ibn.col].fillna("").astype(str)
                + "\x1f"
                + df_dnl[col_inf.col].fillna("").astype(str)
            )
            df_prc  = df_dnl[~fingerprint.isin(tran_ddb)].copy()
            rec_len = len(df_dnl)
            flt_len = len(df_prc)

            st.success(f"✅ {rec_len} Transaktionen zwischen {start_date:%Y-%m-%d} und {end_date} geladen.")
            st.success(f"✅ {rec_len - flt_len} Duplikate entfernt · {flt_len} neue Transaktionen.")
            st.info(f"Aktueller Saldo: {float(balance.amount.amount):,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))

            if flt_len == 0:
                st.info("Keine neuen Buchungen.")
            else:
                df_prc[[col_app.col, col_loc.col]] = (
                    df_prc[col_anm.col]
                    .str.lower()
                    .str.split("/", n=1, expand=True)
                    .reindex(columns=[0, 1])
                )
                df_prc[col_amt.col] = (
                    df_prc[col_amt.col].astype(str).str.extract(r"([-\d.]+)").astype(float)
                )
                df_prc[col_new.col] = True

                ddb_rsl = get_group_category(df_prc[col_app.col].unique().tolist())
                df_prc  = df_prc.merge(ddb_rsl, on=col_app.col, how="left")

                mask = df_prc[col_da1.col] != df_prc[col_da2.col]
                if mask.sum() > 0:
                    st.warning(f"⚠️ {mask.sum()} Zeile(n) mit abweichendem Datum – guessed_entry_date wird genutzt.")
                    with st.expander("Abweichende Zeilen"):
                        st.dataframe(df_prc[mask])
                    df_prc.loc[mask, col_da1.col] = df_prc.loc[mask, col_da2.col]

                df_prc[col_dat.col] = df_prc[[col_dat.col, col_da1.col]].min(axis=1)
                df_prc[col_rid.col] = range(min_id, min_id + flt_len)
                df_prc[col_yea.col] = pd.to_datetime(df_prc[col_dat.col]).dt.year
                df_prc[col_mon.col] = pd.to_datetime(df_prc[col_dat.col]).dt.month
                df_prc[col_ctx.col] = "Alltag"
                df_prc[col_rel.col] = "Familie"
                df_prc[col_spc.col] = False
                df_prc[col_sld.col] = (
                    float(balance.amount.amount)
                    - df_prc[col_amt.col][::-1].cumsum().shift(1).fillna(0)[::-1]
                ).round(2)

                st.session_state["fints_df_prc"]    = df_prc
                st.session_state["fints_downloaded"] = True

                preview_cols = [col_dat, col_app, col_loc, col_amt, col_inf, col_add, col_grp, col_cat, col_sld]
                st.dataframe(df_prc[[c.col for c in preview_cols]], column_config=get_config(preview_cols))

        # Speichern & Export
        if st.session_state.get("fints_downloaded") and "fints_df_prc" in st.session_state:
            df_prc = st.session_state["fints_df_prc"]
            ca, cb = st.columns(2)
            with ca:
                st.button(
                    "💾 In Datenbank speichern",
                    on_click=db_store,
                    args=(df_prc, var_acc, "fints"),
                )
            with cb:
                st.download_button(
                    "📋 CSV herunterladen",
                    data=df_prc.to_csv(index=False).encode(),
                    file_name=f"transactions_{start_date}_{end_date}.csv",
                    mime="text/csv",
                )


# ── Tab: CSV-Import ───────────────────────────────────────────────────────────

with tab_csv:
    if csv_accounts.empty:
        st.info("Keine CSV-Konten vorhanden. Konten können unter ⚙️ Administration hinzugefügt werden.")
    else:
        selected = _render_account_buttons(csv_accounts, "csv")
        if selected is None:
            st.stop()

        _acc_row = csv_accounts[csv_accounts["KEY"] == selected].iloc[0]
        try:
            var_acc = safe_table_name(_acc_row["IBAN"])
        except ValueError as e:
            st.error(f"Ungültiges Konto: {e}")
            st.stop()

        # Datums- & ID-Grenzen aus DB
        start_date = con.sql(f'SELECT MAX("{col_dat.col}"::DATE) FROM "{var_acc}"').fetchone()[0]
        start_date = (start_date - timedelta(days=10)) if start_date is not None else date(1970, 1, 1)
        min_id     = con.sql(f'SELECT MAX("{col_rid.col}") FROM "{var_acc}"').fetchone()[0]
        min_id     = (min_id + 1) if min_id is not None else 0
        end_date   = date.today()

        st.markdown(
            "CSV-Datei der Bank hochladen. Erwartetes Format: Semikolon-getrennt, "
            "Datumsformat `TT.MM.JJJJ`, Dezimaltrennzeichen Komma."
        )
        uploaded = st.file_uploader("CSV-Datei", type=["csv", "txt"], key="csv_upload")

        if uploaded is not None:
            # Datei dekodieren
            raw = uploaded.read()
            content = None
            for enc in ("utf-8-sig", "latin-1", "utf-8"):
                try:
                    content = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if content is None:
                st.error("Datei konnte nicht dekodiert werden (UTF-8/Latin-1 fehlgeschlagen).")
                st.stop()

            # Mehrzeiligen Header überspringen – Daten starten mit "Buchung;"
            lines = content.splitlines()
            header_idx = next(
                (i for i, line in enumerate(lines) if line.strip().startswith("Buchung;")),
                None,
            )
            if header_idx is None:
                st.error(
                    "Keine gültige Spaltenkopfzeile gefunden. "
                    "Erwartet wird eine Zeile die mit 'Buchung;' beginnt."
                )
                st.stop()

            df_raw = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=";", dtype=str)

            # Deutsches Zahlen- und Datumsformat parsen
            def _de_float(s) -> float | None:
                if pd.isna(s) or str(s).strip() in ("", "-"):
                    return None
                try:
                    return float(str(s).strip().replace(".", "").replace(",", "."))
                except ValueError:
                    return None

            def _de_date(s):
                if pd.isna(s) or str(s).strip() == "":
                    return None
                try:
                    return pd.to_datetime(str(s).strip(), format="%d.%m.%Y").date()
                except Exception:
                    return None

            # Spalten-Mapping
            # CSV:  Buchung ; Wertstellungsdatum ; Auftraggeber/Empfänger ;
            #       Buchungstext ; Verwendungszweck ; Saldo ; Währung ; Betrag ; Währung
            rename_map = {k: v for k, v in {
                "Buchung":                col_dat.col,
                "Wertstellungsdatum":     col_da1.col,
                "Auftraggeber/Empfänger": col_anm.col,
                "Buchungstext":           col_add.col,
                "Verwendungszweck":       col_inf.col,
                "Saldo":                  col_sld.col,
                "Betrag":                 col_amt.col,
            }.items() if k in df_raw.columns}
            df_prc = df_raw.rename(columns=rename_map).copy()

            df_prc[col_dat.col] = df_prc[col_dat.col].apply(_de_date)
            df_prc[col_da1.col] = df_prc[col_da1.col].apply(_de_date)
            df_prc[col_amt.col] = df_prc[col_amt.col].apply(_de_float)
            df_prc[col_sld.col] = df_prc[col_sld.col].apply(_de_float)

            # Zeilen ohne Datum oder Betrag entfernen (Fußnoten, Leerzeilen)
            df_prc = df_prc.dropna(subset=[col_dat.col, col_amt.col]).copy()

            if df_prc.empty:
                st.warning("Keine verwertbaren Buchungszeilen gefunden.")
                st.stop()

            rec_len = len(df_prc)

            # Empfänger → applicant + location
            df_prc[[col_app.col, col_loc.col]] = (
                df_prc[col_anm.col]
                .fillna("")
                .str.lower()
                .str.split("/", n=1, expand=True)
                .reindex(columns=[0, 1])
            )

            # guessed_entry_date = Wertstellungsdatum (kein FinTS-Guess nötig)
            df_prc[col_da2.col] = df_prc[col_da1.col]
            # Buchungsdatum = Minimum aus Buchung und Wertstellung
            df_prc[col_dat.col] = df_prc[[col_dat.col, col_da1.col]].min(axis=1)

            df_prc[col_yea.col] = pd.to_datetime(df_prc[col_dat.col]).dt.year
            df_prc[col_mon.col] = pd.to_datetime(df_prc[col_dat.col]).dt.month
            df_prc[col_ctx.col] = "Alltag"
            df_prc[col_rel.col] = "Familie"
            df_prc[col_new.col] = True
            df_prc[col_spc.col] = False
            df_prc[col_brf.col] = None
            df_prc[col_eer.col] = None
            df_prc[col_ibn.col] = None

            # Gruppe/Kategorie aus bekannten Empfängern
            ddb_rsl = get_group_category(df_prc[col_app.col].dropna().unique().tolist())
            df_prc  = df_prc.merge(ddb_rsl, on=col_app.col, how="left")

            # Duplikat-Prüfung: Datum + Betrag + Verwendungszweck
            csv_fp = (
                df_prc[col_dat.col].astype(str)
                + df_prc[col_amt.col].astype(str)
                + df_prc[col_inf.col].fillna("").astype(str)
            )
            csv_min_date = df_prc[col_dat.col].min()
            tran_csv_ddb = set(
                con.execute(
                    f'SELECT CONCAT("{col_dat.col}"::TEXT, "{col_amt.col}"::TEXT, '
                    f'COALESCE("{col_inf.col}", \'\')) '
                    f'FROM "{var_acc}" WHERE "{col_dat.col}"::DATE >= ?',
                    [str(csv_min_date)],
                ).df().iloc[:, 0]
            )
            df_prc  = df_prc[~csv_fp.isin(tran_csv_ddb)].copy()
            flt_len = len(df_prc)

            st.success(
                f"✅ {rec_len} Zeilen gelesen · "
                f"{rec_len - flt_len} Duplikate entfernt · "
                f"{flt_len} neue Buchungen."
            )

            if flt_len == 0:
                st.info("Keine neuen Buchungen.")
            else:
                df_prc = df_prc.iloc[::-1].reset_index(drop=True)
                df_prc[col_rid.col] = range(min_id, min_id + flt_len)

                st.session_state["csv_df_prc"]    = df_prc
                st.session_state["csv_downloaded"] = True

                preview_cols = [col_dat, col_app, col_loc, col_amt, col_inf, col_add, col_grp, col_cat, col_sld]
                st.dataframe(
                    df_prc[[c.col for c in preview_cols if c.col in df_prc.columns]],
                    column_config=get_config(preview_cols),
                )

            # Speichern & Export
            if st.session_state.get("csv_downloaded") and "csv_df_prc" in st.session_state:
                df_prc = st.session_state["csv_df_prc"]
                ca, cb = st.columns(2)
                with ca:
                    st.button(
                        "💾 In Datenbank speichern",
                        on_click=db_store,
                        args=(df_prc, var_acc, "csv"),
                        key="csv_db_save",
                    )
                with cb:
                    st.download_button(
                        "📋 CSV herunterladen",
                        data=df_prc.to_csv(index=False).encode(),
                        file_name=f"transactions_csv_{start_date}_{end_date}.csv",
                        mime="text/csv",
                        key="csv_dl",
                    )
