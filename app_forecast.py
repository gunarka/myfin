"""
app_forecast.py
Prognosefunktion: wiederkehrende Buchungen, einmalige Ereignisse, What-If-Analyse,
Konfidenzbänder, Inflationsmodell, Liquiditätswarnungen, Szenarien und Sparzielen.
SICHERHEIT: Alle DB-Zugriffe parametrisiert; IBANs gegen Whitelist geprüft.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import io
import re
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import date

from app_functions import (
    require_master_password,
    list_saved_users,
    load_categories,
    colour_amount,
    get_config,
    con,
    # Forecast-Funktionen
    ensure_forecast_tables,
    load_recurring, save_recurring, update_recurring, delete_recurring,
    load_oneoff, save_oneoff, update_oneoff, delete_oneoff,
    load_inflation, upsert_inflation,
    list_scenarios, save_scenario, load_scenario, delete_scenario,
    is_reserved_scenario_name,
    detect_recurring, category_average, distinct_field_values,
    compute_forecast, liquidity_warnings, forecast_vs_actual,
    # Historische Inflation (Referenzdaten)
    load_inflation_history, list_inflation_history_categories,
    upsert_inflation_history, delete_inflation_history_category,
    average_yoy_inflation, COICOP_DIVISIONS,
    INTERVAL_TYPES, STATUS_TYPES,
    # Spalten-Definitionen für Tabellen-Anzeige
    col_app, col_amt, col_grp, col_cat, col_rel, col_ctx, col_iban, col_spc,
    col_int_typ, col_int_num, col_st_dat, col_en_dat, col_status,
    col_var_pct, col_note, col_oo_dat,
    DASHBOARD_COLORS, make_plotly_theme,
)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("🔮 Vorhersagen")


# ── Design-System (zentral in app_functions.py, hier nur referenziert) ───────
C = DASHBOARD_COLORS

PLOTLY_THEME = make_plotly_theme()

PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "toImage"],
}

PLOTLY_CONFIG_RADIAL = {
    "displaylogo": False,
    "modeBarButtonsToAdd": ["toImage", "resetViews"],
    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
}

CHART_HEIGHT = 420


# ── Destatis-Flat-File-Erkennung (GENESIS-Online CSV-Export) ─────────────────
# Format: eine Zeile pro Merkmalskombination/Zeitpunkt, u.a. mit den Spalten
# "2_variable_attribute_code" (COICOP-Code, z.B. "CC13-07223") und
# "2_variable_attribute_label" (Klartext-Label). Der Ziffernteil nach dem "-"
# codiert die Gliederungstiefe (2 = Abteilung, 4 = Gruppe, 5 = Einzelposition);
# die ersten 2 Ziffern sind immer die übergeordnete Abteilung.

_DESTATIS_FLAT_COLS = {
    "time_code", "time", "2_variable_code", "2_variable_attribute_code",
    "2_variable_attribute_label", "value",
}

_COICOP_LEVEL_LABELS = {
    2: "2-Steller (Abteilungen)",
    3: "3-Steller",
    4: "4-Steller (Gruppen)",
    5: "5-Steller (Einzelpositionen)",
}


def _is_destatis_flat(df: pd.DataFrame) -> bool:
    return _DESTATIS_FLAT_COLS.issubset(set(df.columns))


def _destatis_num(s) -> float | None:
    """Deutsches Zahlenformat parsen; Destatis-Platzhalter für fehlende Werte
    ('.', '-', 'x', '...') als None behandeln."""
    s = str(s).strip()
    if s in ("", ".", "-", "x", "X", "...", "..", "/"):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _coicop_level(code) -> int:
    m = re.search(r"-(\d+)$", str(code))
    return len(m.group(1)) if m else 0


def _coicop_division(code) -> str | None:
    m = re.match(r"CC13-(\d{2})", str(code))
    return m.group(1) if m else None


# ── Stammdaten für Auswahllisten ──────────────────────────────────────────────
accounts = list_saved_users()
all_ibans = accounts["IBAN"].tolist()
iban_label_map = {
    row["IBAN"]: f"{row['Person']} · {row['Bank']} · {row['Konto']}"
    for _, row in accounts.iterrows()
}
def iban_fmt(iban: str) -> str:
    return iban_label_map.get(iban, iban or "—")

cat_df    = load_categories()
all_grps  = sorted(cat_df["group"].unique().tolist(), key=str.lower)
all_cats  = sorted(cat_df["category"].unique().tolist(), key=str.lower)
all_combos = sorted(
    [f"{r['group']} · {r['category']}" for _, r in cat_df.iterrows()],
    key=str.lower,
)


# ── Tab-Struktur ──────────────────────────────────────────────────────────────
tab_manage, tab_forecast, tab_warn, tab_compare, tab_scenarios = st.tabs([
    "📋 Verwalten",
    "🔮 Vorhersage",
    "⚠️ Warnungen",
    "🔄 Prognose vs. Ist",
    "💾 Szenarien",
])


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1: Verwalten
# ═══════════════════════════════════════════════════════════════════════════════
with tab_manage:
    sub_rec, sub_one, sub_auto, sub_avg, sub_infl = st.tabs([
        "Wiederkehrend",
        "Einmalig",
        "🔍 Auto-Erkennung",
        "📊 Aus Mittelwert",
        "📈 Inflation",
    ])

    # distinct_field_values scannt alle IBAN-Tabellen – einmalig für alle Sub-Tabs berechnen
    _all_rels = [""] + distinct_field_values(col_rel.col)
    _all_ctxs = [""] + distinct_field_values(col_ctx.col)

    # ── Wiederkehrende Buchungen ──────────────────────────────────────────────
    with sub_rec:
        st.subheader("Wiederkehrende Buchungen")

        with st.expander("➕ Neue wiederkehrende Buchung anlegen"):
            with st.form("rec_new", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                with c1:
                    f_app  = st.text_input(col_app.lab, key="rn_app")
                    f_amt  = st.number_input(col_amt.lab, value=0.0, step=10.0,
                                              format="%.2f", key="rn_amt")
                    f_iban = st.selectbox(col_iban.lab, options=[""] + all_ibans,
                                           format_func=iban_fmt, key="rn_iban")
                with c2:
                    f_grp_cat = st.selectbox(
                        f"{col_grp.lab} · {col_cat.lab}",
                        options=[""] + all_combos, key="rn_grp_cat",
                    )
                    f_rel  = st.text_input(col_rel.lab, value="Familie",   key="rn_rel")
                    f_ctx  = st.text_input(col_ctx.lab, value="Alltag",    key="rn_ctx")
                with c3:
                    f_typ  = st.selectbox(col_int_typ.lab, INTERVAL_TYPES,
                                           index=2, key="rn_typ")
                    f_num  = st.number_input(col_int_num.lab, min_value=1, value=1, key="rn_num")
                    f_sdt  = st.date_input(col_st_dat.lab, value=date.today(), key="rn_sdt")
                    f_edt  = st.date_input(col_en_dat.lab, value=None, key="rn_edt")
                    f_var  = st.number_input(col_var_pct.lab, min_value=0.0, max_value=100.0,
                                              value=10.0, step=1.0, key="rn_var")
                f_note = st.text_input(col_note.lab, key="rn_note")
                if st.form_submit_button("💾 Speichern", width="stretch"):
                    if not f_app or f_amt == 0:
                        st.error("Empfänger und Betrag sind Pflichtfelder.")
                    else:
                        _f_grp, _f_cat = (
                            f_grp_cat.split(" · ", 1) if " · " in (f_grp_cat or "") else (None, None)
                        )
                        save_recurring({
                            "applicant": f_app, "amount": f_amt,
                            "group": _f_grp, "category": _f_cat,
                            "relation": f_rel or None, "context": f_ctx or None,
                            "iban": f_iban or None,
                            "interval_type": f_typ, "interval_num": int(f_num),
                            "start_date": f_sdt, "end_date": f_edt,
                            "status": "aktiv", "variability": float(f_var),
                            "note": f_note or None,
                        })
                        st.success("✅ Eintrag gespeichert.")
                        st.rerun()

        # ── Bearbeitbare Liste ────────────────────────────────────────────────
        rec_df = load_recurring()
        if rec_df.empty:
            st.info("Noch keine wiederkehrenden Buchungen angelegt.")
        else:
            st.caption(f"{len(rec_df)} Einträge · Bearbeitung in der Tabelle, "
                       "anschließend mit „Speichern“ übernehmen.")

            # grp_cat-Spalte einfügen; group/category im Editor ausblenden
            rec_display = rec_df.copy()
            rec_display["grp_cat"] = [
                f"{g} · {c}" if g and c else None
                for g, c in zip(
                    rec_display["group"].fillna(""),
                    rec_display["category"].fillna(""),
                )
            ]
            _dcols = list(rec_display.columns)
            _dcols.remove("grp_cat")
            _dcols.insert(_dcols.index("group"), "grp_cat")
            rec_display = rec_display[_dcols]

            rec_display.insert(0, "entfernen", False)

            edited = st.data_editor(
                rec_display.style.map(colour_amount, subset=["amount"]).format({
                    "amount": "{:,.2f} €",
                    "variability": "{:.1f} %",
                }),
                hide_index=True,
                num_rows="fixed",
                disabled=["forecast_id"],
                column_config={
                    "entfernen":   st.column_config.CheckboxColumn("Entfernen"),
                    "forecast_id": None,
                    "applicant":   st.column_config.TextColumn(col_app.lab),
                    "amount":      st.column_config.NumberColumn(col_amt.lab,
                                       format="%.2f €", step=0.01),
                    "group":       None,
                    "category":    None,
                    "grp_cat":     st.column_config.SelectboxColumn(
                                       f"{col_grp.lab} · {col_cat.lab}",
                                       options=[""] + all_combos,
                                   ),
                    "relation":    st.column_config.SelectboxColumn(col_rel.lab,
                                       options=_all_rels),
                    "context":     st.column_config.SelectboxColumn(col_ctx.lab,
                                       options=_all_ctxs),
                    "iban":        st.column_config.SelectboxColumn(col_iban.lab,
                                       options=[""] + all_ibans),
                    "interval_type": st.column_config.SelectboxColumn(col_int_typ.lab,
                                        options=INTERVAL_TYPES),
                    "interval_num":  st.column_config.NumberColumn(col_int_num.lab,
                                        min_value=1, step=1),
                    "start_date":  st.column_config.DateColumn(col_st_dat.lab,
                                       format="DD.MM.YYYY"),
                    "end_date":    st.column_config.DateColumn(col_en_dat.lab,
                                       format="DD.MM.YYYY"),
                    "status":      st.column_config.SelectboxColumn(col_status.lab,
                                       options=STATUS_TYPES),
                    "variability": st.column_config.NumberColumn(col_var_pct.lab,
                                       min_value=0.0, max_value=100.0, step=1.0,
                                       format="%.1f %%"),
                    "note":        st.column_config.TextColumn(col_note.lab),
                },
                key="rec_editor",
                width="stretch",
            )

            # ── Änderungen ermitteln ──────────────────────────────────────────
            # Wie in app_assign.py: nur die von Streamlit selbst protokollierten
            # Edits verwenden (st.session_state["rec_editor"]["edited_rows"]),
            # statt eines vollständigen DataFrame-Vergleichs. Ein .compare() über
            # das gesamte Grid erkennt Zeilen fälschlich als "geändert", wenn
            # Streamlit eine SelectboxColumn-Zelle (hier: grp_cat) bei einem
            # Rerun intern zurücksetzt, weil sie kurzzeitig nicht exakt in der
            # aktuellen options-Liste enthalten ist.
            _rec_edits = st.session_state.get("rec_editor", {}).get("edited_rows", {})

            to_delete_ids = [
                int(rec_display.iloc[int(pos)]["forecast_id"])
                for pos, changed in _rec_edits.items() if changed.get("entfernen")
            ]

            _rec_updates: dict[int, dict] = {}
            for pos, changed in _rec_edits.items():
                if changed.get("entfernen"):
                    continue
                fid = int(rec_display.iloc[int(pos)]["forecast_id"])
                fields = {}
                if "grp_cat" in changed:
                    val = changed["grp_cat"] or ""
                    g, c = val.split(" · ", 1) if " · " in val else (None, None)
                    fields["group"] = g or None
                    fields["category"] = c or None
                for c_name in ("applicant", "amount", "relation", "context", "iban",
                               "interval_type", "interval_num", "start_date", "end_date",
                               "status", "variability", "note"):
                    if c_name in changed:
                        fields[c_name] = changed[c_name]
                if fields:
                    _rec_updates[fid] = fields

            has_changes = len(to_delete_ids) > 0 or len(_rec_updates) > 0
            if st.button("💾 Änderungen speichern",
                          disabled=not has_changes,
                          width="stretch"):
                for fid in to_delete_ids:
                    delete_recurring(fid)
                for fid, fields in _rec_updates.items():
                    if fid in to_delete_ids:
                        continue
                    update_recurring(fid, fields)
                msg_parts = []
                if to_delete_ids:
                    msg_parts.append(f"{len(to_delete_ids)} gelöscht")
                if _rec_updates:
                    msg_parts.append(f"{len(_rec_updates)} aktualisiert")
                st.success(f"✅ {', '.join(msg_parts)}.")
                st.rerun()

    # ── Einmalige Ereignisse ──────────────────────────────────────────────────
    with sub_one:
        st.subheader("Einmalige geplante Ereignisse")
        st.caption("Anschaffungen, Urlaube, Bonuszahlungen – alles was nicht regelmäßig auftritt.")

        with st.expander("➕ Neues einmaliges Ereignis"):
            with st.form("one_new", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                with c1:
                    o_app  = st.text_input(col_app.lab, key="on_app")
                    o_amt  = st.number_input(col_amt.lab, value=0.0, step=50.0, format="%.2f", key="on_amt")
                    o_dat  = st.date_input(col_oo_dat.lab, value=date.today(), key="on_dat")
                with c2:
                    o_grp_cat = st.selectbox(
                        f"{col_grp.lab} · {col_cat.lab}",
                        options=[""] + all_combos, key="on_grp_cat",
                    )
                    o_iban = st.selectbox(col_iban.lab, options=[""] + all_ibans,
                                           format_func=iban_fmt, key="on_iban")
                with c3:
                    o_rel = st.selectbox(col_rel.lab, options=_all_rels, key="on_rel")
                    o_ctx = st.selectbox(col_ctx.lab, options=_all_ctxs, key="on_ctx")
                o_note = st.text_input(col_note.lab, key="on_note")
                if st.form_submit_button("💾 Speichern", width="stretch"):
                    if not o_app or o_amt == 0:
                        st.error("Empfänger und Betrag sind Pflichtfelder.")
                    else:
                        _o_grp, _o_cat = (
                            o_grp_cat.split(" · ", 1) if " · " in (o_grp_cat or "") else (None, None)
                        )
                        save_oneoff({
                            "applicant": o_app, "amount": o_amt,
                            "group": _o_grp, "category": _o_cat,
                            "relation": o_rel or None, "context": o_ctx or None,
                            "iban":  o_iban or None,
                            "event_date": o_dat, "note": o_note or None,
                        })
                        st.success("✅ Ereignis gespeichert.")
                        st.rerun()

        one_df = load_oneoff()
        if one_df.empty:
            st.info("Noch keine einmaligen Ereignisse angelegt.")
        else:
            one_display = one_df.copy()
            one_display.insert(0, "entfernen", False)
            edited_one = st.data_editor(
                one_display.style.map(colour_amount, subset=["amount"])
                                 .format({"amount": "{:,.2f} €"}),
                hide_index=True,
                num_rows="fixed",
                disabled=["oneoff_id"],
                column_config={
                    "entfernen":  st.column_config.CheckboxColumn("Entfernen"),
                    "oneoff_id":  None,
                    "applicant":  st.column_config.TextColumn(col_app.lab),
                    "amount":     st.column_config.NumberColumn(col_amt.lab, format="%.2f €"),
                    "group":      st.column_config.TextColumn(col_grp.lab),
                    "category":   st.column_config.TextColumn(col_cat.lab),
                    "relation":   st.column_config.SelectboxColumn(col_rel.lab, options=_all_rels),
                    "context":    st.column_config.SelectboxColumn(col_ctx.lab, options=_all_ctxs),
                    "iban":       st.column_config.TextColumn(col_iban.lab),
                    "event_date": st.column_config.DateColumn(col_oo_dat.lab, format="DD.MM.YYYY"),
                    "note":       st.column_config.TextColumn(col_note.lab),
                },
                key="one_editor",
                width="stretch",
            )
            # ── Änderungen ermitteln (edited_rows statt .compare(), siehe oben) ──
            _one_edits = st.session_state.get("one_editor", {}).get("edited_rows", {})

            to_del_oids = [
                int(one_display.iloc[int(pos)]["oneoff_id"])
                for pos, changed in _one_edits.items() if changed.get("entfernen")
            ]

            _one_updates: dict[int, dict] = {}
            for pos, changed in _one_edits.items():
                if changed.get("entfernen"):
                    continue
                oid = int(one_display.iloc[int(pos)]["oneoff_id"])
                fields = {c: v for c, v in changed.items() if c != "entfernen"}
                if fields:
                    _one_updates[oid] = fields

            has_changes = len(to_del_oids) > 0 or len(_one_updates) > 0
            if st.button("💾 Änderungen speichern",
                          disabled=not has_changes,
                          key="one_save_btn", width="stretch"):
                for oid in to_del_oids:
                    delete_oneoff(oid)
                for oid, fields in _one_updates.items():
                    if oid in to_del_oids:
                        continue
                    update_oneoff(oid, fields)
                msg_parts = []
                if to_del_oids:
                    msg_parts.append(f"{len(to_del_oids)} gelöscht")
                if _one_updates:
                    msg_parts.append(f"{len(_one_updates)} aktualisiert")
                st.success(f"✅ {', '.join(msg_parts)}.")
                st.rerun()

            if st.button("🗑️ Nur Löschen (keine weiteren Updates)", disabled=len(to_del_oids) == 0,
                          key="one_del_btn", type="secondary", width="stretch"):
                for oid in to_del_oids:
                    delete_oneoff(oid)
                st.success(f"✅ {len(to_del_oids)} Ereignis/Ereignisse gelöscht.")
                st.rerun()

    # ── Auto-Erkennung ────────────────────────────────────────────────────────
    with sub_auto:
        st.subheader("Wiederkehrende Buchungen automatisch erkennen")
        st.caption(
            "Lokale Heuristik: durchsucht alle Konten nach Empfängern mit "
            "regelmäßigem Buchungsabstand. Mindestens 3 Vorkommen, kein Versand "
            "an externe Dienste."
        )
        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            lookback = st.number_input("Zeitfenster (Monate)", 3, 60, 12, key="auto_lookback")
        with c2:
            min_occ  = st.number_input("Mind. Vorkommen", 2, 12, 3, key="auto_min_occ")
        with c3:
            st.markdown("<br>", unsafe_allow_html=True)
            run = st.button("🔍 Analyse starten", width="stretch")

        if run:
            with st.spinner("Analysiere Transaktionen…"):
                sugg = detect_recurring(int(lookback), int(min_occ))
            st.session_state["auto_suggestions"] = sugg

        sugg = st.session_state.get("auto_suggestions", pd.DataFrame())
        if not sugg.empty:
            # Ausschluss bereits erfasster Empfänger (gleiche IBAN + applicant + Vorzeichen)
            existing = load_recurring()
            if not existing.empty:
                ex_keys = set(zip(
                    existing["applicant"].fillna(""), existing["iban"].fillna(""),
                    existing["amount"].apply(lambda x: 1 if x >= 0 else -1),
                ))
                sugg["_existing"] = sugg.apply(
                    lambda r: (r["applicant"], r["iban"] or "", 1 if r["amount"] >= 0 else -1) in ex_keys,
                    axis=1,
                )
                sugg = sugg[~sugg["_existing"]].drop(columns=["_existing"])

            if sugg.empty:
                st.info("Keine neuen Vorschläge – alle erkannten Muster sind bereits erfasst.")
            else:
                st.success(f"{len(sugg)} Muster gefunden – auswählen und übernehmen:")
                sugg_disp = sugg.copy()
                sugg_disp.insert(0, "übernehmen", False)
                edited_sugg = st.data_editor(
                    sugg_disp,
                    hide_index=True,
                    column_config={
                        "übernehmen":      st.column_config.CheckboxColumn("✓"),
                        "applicant":       st.column_config.TextColumn(col_app.lab),
                        "amount":          st.column_config.NumberColumn(col_amt.lab, format="%.2f €"),
                        "iban":            st.column_config.TextColumn(col_iban.lab),
                        "group":           st.column_config.SelectboxColumn(col_grp.lab, options=[""] + all_grps),
                        "category":        st.column_config.SelectboxColumn(col_cat.lab, options=[""] + all_cats),
                        "interval_type":   st.column_config.SelectboxColumn(col_int_typ.lab, options=INTERVAL_TYPES),
                        "interval_num":    st.column_config.NumberColumn(col_int_num.lab, min_value=1),
                        "occurrences":     st.column_config.NumberColumn("Vorkommen"),
                        "variability_pct": st.column_config.NumberColumn(col_var_pct.lab, format="%.1f %%"),
                        "last_seen":       st.column_config.DateColumn("Zuletzt", format="DD.MM.YYYY"),
                    },
                    width="stretch", key="auto_editor",
                )
                if st.button("📥 Ausgewählte übernehmen", width="stretch"):
                    chosen = edited_sugg[edited_sugg["übernehmen"]]
                    n_added = 0
                    for _, r in chosen.iterrows():
                        save_recurring({
                            "applicant":     r["applicant"],
                            "amount":        float(r["amount"]),
                            "group":         r["group"] or None,
                            "category":      r["category"] or None,
                            "relation":      "Familie",
                            "context":       "Alltag",
                            "iban":          r["iban"] or None,
                            "interval_type": r["interval_type"],
                            "interval_num":  int(r["interval_num"]),
                            "start_date":    date.today(),
                            "end_date":      None,
                            "status":        "aktiv",
                            "variability":   float(r["variability_pct"]),
                            "note":          f"Auto-erkannt aus {int(r['occurrences'])} Buchungen",
                        })
                        n_added += 1
                    if n_added:
                        st.success(f"✅ {n_added} Eintrag/Einträge übernommen.")
                        st.session_state.pop("auto_suggestions", None)
                        st.rerun()
                    else:
                        st.warning("Keine Zeile ausgewählt.")
        elif "auto_suggestions" in st.session_state:
            st.info("Keine wiederkehrenden Muster gefunden.")

    # ── Aus Mittelwert anlegen ────────────────────────────────────────────────
    with sub_avg:
        st.subheader("Wiederkehrenden Eintrag aus Kategorien-Mittelwert anlegen")
        st.caption(
            "Berechnet den durchschnittlichen Monatsbetrag einer Gruppe/Kategorie "
            "über die letzten N Monate und legt einen monatlichen Eintrag an. "
            "Nützlich für variable Kosten wie Lebensmittel oder Tanken."
        )
        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
        with c1:
            ag_grp = st.selectbox(col_grp.lab, options=[""] + all_grps, key="ag_grp")
        with c2:
            cat_options = sorted(
                cat_df[cat_df["group"] == ag_grp]["category"].tolist(), key=str.lower
            ) if ag_grp else all_cats
            ag_cat = st.selectbox(col_cat.lab, options=[""] + cat_options, key="ag_cat")
        with c3:
            ag_months = st.number_input("Monate Ø", 1, 36, 6, key="ag_months")
        with c4:
            st.markdown("<br>", unsafe_allow_html=True)
            calc = st.button("Berechnen", width="stretch")

        c1, c2, c3 = st.columns(3)
        with c1:
            ag_rel = st.selectbox(col_rel.lab, options=_all_rels, key="ag_rel")
        with c2:
            ag_ctx = st.selectbox(col_ctx.lab, options=_all_ctxs, key="ag_ctx")
        with c3:
            ag_spc = st.selectbox(
                col_spc.lab, options=["Alle", "Ja", "Nein"], key="ag_spc"
            )

        if calc and ag_grp:
            _special = {"Ja": True, "Nein": False}.get(ag_spc)
            avg = category_average(
                ag_grp, ag_cat or None, int(ag_months),
                relation=ag_rel or None,
                context=ag_ctx or None,
                special=_special,
            )
            label_parts = [ag_grp] + ([ag_cat] if ag_cat else [])
            label = "Ø " + " / ".join(label_parts)
            st.session_state["ag_avg"]        = avg
            st.session_state["ag_label"]      = label
            st.session_state["ag_result_grp"] = ag_grp
            st.session_state["ag_result_cat"] = ag_cat or None

        if "ag_avg" in st.session_state:
            avg = st.session_state["ag_avg"]
            _saved_grp = st.session_state.get("ag_result_grp")
            _saved_cat = st.session_state.get("ag_result_cat")
            _rel_default = st.session_state.get("ag_rel") or ""
            _ctx_default = st.session_state.get("ag_ctx") or ""
            st.info(f"Mittelwert: **{avg:,.2f} €** pro Monat über die letzten {int(ag_months)} Monate.")
            with st.form("ag_save", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                with c1:
                    ag_app = st.text_input(col_app.lab, value=st.session_state["ag_label"])
                    ag_iban = st.selectbox(col_iban.lab, options=[""] + all_ibans,
                                            format_func=iban_fmt)
                with c2:
                    ag_amt = st.number_input(col_amt.lab, value=float(avg), format="%.2f")
                    ag_var = st.number_input(col_var_pct.lab, min_value=0.0, max_value=100.0,
                                              value=20.0, step=5.0)
                with c3:
                    ag_sdt = st.date_input(col_st_dat.lab, value=date.today())
                    ag_edt = st.date_input(col_en_dat.lab, value=None)
                c1, c2 = st.columns(2)
                with c1:
                    ag_save_rel = st.selectbox(
                        col_rel.lab, options=_all_rels,
                        index=_all_rels.index(_rel_default) if _rel_default in _all_rels else 0,
                    )
                with c2:
                    ag_save_ctx = st.selectbox(
                        col_ctx.lab, options=_all_ctxs,
                        index=_all_ctxs.index(_ctx_default) if _ctx_default in _all_ctxs else 0,
                    )
                if st.form_submit_button("💾 Als wiederkehrend anlegen", width="stretch"):
                    save_recurring({
                        "applicant": ag_app, "amount": ag_amt,
                        "group": _saved_grp, "category": _saved_cat,
                        "relation": ag_save_rel or None, "context": ag_save_ctx or None,
                        "iban": ag_iban or None,
                        "interval_type": "monatlich", "interval_num": 1,
                        "start_date": ag_sdt, "end_date": ag_edt,
                        "status": "aktiv", "variability": float(ag_var),
                        "note": f"Ø über {int(ag_months)} Monate",
                    })
                    st.success("✅ Mittelwert-Eintrag angelegt.")
                    st.session_state.pop("ag_avg", None)
                    st.rerun()

    # ── Inflation pro Gruppe ──────────────────────────────────────────────────
    with sub_infl:
        st.subheader("Jährliche Steigerung pro Gruppe")
        st.caption(
            "Steigerungssätze werden in der Prognose über die Zeit kumuliert "
            "(z.B. 8 % p.a. auf Energie). Wirkt nur auf wiederkehrende Buchungen."
        )
        infl_df = load_inflation()
        # Alle Gruppen anzeigen, auch die ohne Eintrag (= 0 %)
        infl_full = pd.DataFrame({"group": all_grps}).merge(
            infl_df, on="group", how="left"
        ).fillna({"annual_pct": 0.0})

        edited_infl = st.data_editor(
            infl_full, hide_index=True,
            column_config={
                "group":      st.column_config.TextColumn(col_grp.lab, disabled=True),
                "annual_pct": st.column_config.NumberColumn("Steigerung p.a.",
                                  min_value=-50.0, max_value=50.0, step=0.5, format="%.1f %%"),
            },
            key="infl_editor", width="stretch",
        )
        if st.button("💾 Inflations-Werte speichern"):
            for _, r in edited_infl.iterrows():
                upsert_inflation(r["group"], float(r["annual_pct"]))
            st.success("✅ Gespeichert.")
            st.rerun()

        # ── Historische Referenzwerte (z.B. Destatis-VPI) ───────────────────────
        st.divider()
        st.subheader("📜 Historische Referenzwerte")
        st.caption(
            "Verbraucherpreisentwicklung nach Kategorie als Orientierung für die "
            "Steigerungssätze oben. Daten manuell z.B. von "
            "[GENESIS-Online](https://genesis.destatis.de) (Tabelle 61111-0001, "
            "12 COICOP-Abteilungen, CSV-Export) herunterladen und hier importieren – "
            "kein automatischer externer Abruf."
        )

        with st.expander("📂 CSV importieren"):
            ih_upload = st.file_uploader(
                "Destatis-Flat-File-CSV (GENESIS-Online) oder beliebige CSV mit "
                "Kategorie/Datum/Wert-Spalten",
                type=["csv", "txt"], key="ih_upload",
            )
            if ih_upload is not None:
                raw = ih_upload.read()
                content = None
                for enc in ("utf-8-sig", "latin-1", "utf-8"):
                    try:
                        content = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    st.error("Datei konnte nicht dekodiert werden (UTF-8/Latin-1 fehlgeschlagen).")
                else:
                    _first_line = content.splitlines()[0] if content else ""
                    sep = ";" if _first_line.count(";") >= _first_line.count(",") else ","
                    try:
                        df_ih_raw = pd.read_csv(io.StringIO(content), sep=sep, dtype=str)
                    except Exception as e:
                        df_ih_raw = None
                        st.error(f"CSV konnte nicht gelesen werden: {e}")

                    if df_ih_raw is not None and not df_ih_raw.empty and _is_destatis_flat(df_ih_raw):
                        # ── Destatis-Flat-File: Gliederung/Hierarchie automatisch nutzen ──
                        st.success("✅ Destatis-Flat-File-Format erkannt – Spalten automatisch zugeordnet.")
                        df_d = df_ih_raw.copy()
                        df_d["_level"]    = df_d["2_variable_attribute_code"].apply(_coicop_level)
                        df_d["_division"] = df_d["2_variable_attribute_code"].apply(_coicop_division)

                        _level_opts = sorted(df_d["_level"].unique())
                        c1, c2 = st.columns(2)
                        with c1:
                            sel_levels = st.multiselect(
                                "Gliederungsebene(n)", options=_level_opts,
                                default=[lv for lv in _level_opts if lv == 2] or _level_opts[:1],
                                format_func=lambda lv: _COICOP_LEVEL_LABELS.get(lv, f"{lv}-Steller"),
                                key="ih_levels",
                                help="2-Steller = die 12 Hauptabteilungen; 4-/5-Steller = "
                                     "feinere Unterkategorien innerhalb einer Abteilung.",
                            )
                        with c2:
                            _time_codes = sorted(df_d["time_code"].unique())
                            sel_time_code = st.selectbox(
                                "Zeitbezug", _time_codes, key="ih_time_code",
                                help="JAHR = Jahreswerte, MONAT = Monatswerte",
                            )

                        _div_labels = {
                            code: COICOP_DIVISIONS.get(code, code)
                            for code in sorted(df_d["_division"].dropna().unique())
                        }
                        sel_divisions = st.multiselect(
                            "Abteilungen einschränken (leer = alle)",
                            options=list(_div_labels.keys()),
                            format_func=lambda c: f"{c} – {_div_labels.get(c, c)}",
                            key="ih_divisions",
                        )

                        if st.button("📥 Importieren", key="ih_import_destatis_btn"):
                            mask = df_d["_level"].isin(sel_levels) & (df_d["time_code"] == sel_time_code)
                            if sel_divisions:
                                mask &= df_d["_division"].isin(sel_divisions)
                            df_sel = df_d[mask].copy()

                            df_sel["value_num"] = df_sel["value"].apply(_destatis_num)
                            n_before = len(df_sel)
                            df_sel = df_sel.dropna(subset=["value_num"])
                            n_skipped = n_before - len(df_sel)

                            if df_sel.empty:
                                st.warning(
                                    "Keine verwertbaren Werte in der Auswahl "
                                    "(nur '.', '-' o.ä. als Platzhalter für fehlende Werte)."
                                )
                            else:
                                if sel_time_code == "JAHR":
                                    df_sel["date"] = pd.to_datetime(
                                        df_sel["time"].astype(str) + "-01-01", errors="coerce"
                                    )
                                else:
                                    df_sel["date"] = pd.to_datetime(df_sel["time"], errors="coerce")
                                n_before2 = len(df_sel)
                                df_sel = df_sel.dropna(subset=["date"])
                                n_skipped += n_before2 - len(df_sel)

                                df_sel["category_code"] = df_sel["2_variable_attribute_code"]
                                df_sel["category"]      = df_sel["2_variable_attribute_label"]
                                df_sel["group_code"]    = df_sel["_division"]
                                df_sel["group_label"]   = df_sel["_division"].map(COICOP_DIVISIONS)
                                df_sel["level"]         = df_sel["_level"]
                                df_sel["index_value"]   = df_sel["value_num"]
                                df_sel["yoy_pct"]       = None

                                n = upsert_inflation_history(df_sel[[
                                    "category_code", "category", "group_code", "group_label",
                                    "level", "date", "index_value", "yoy_pct",
                                ]])
                                st.success(
                                    f"✅ {n} Zeitreihenpunkte importiert/aktualisiert"
                                    + (f" · {n_skipped} übersprungen (ohne Wert/Datum)." if n_skipped else ".")
                                )
                                st.rerun()

                    elif df_ih_raw is not None and not df_ih_raw.empty:
                        # ── Generisches CSV: manuelles Spalten-Mapping ──
                        st.caption(f"{len(df_ih_raw)} Zeilen gelesen · Spalten zuordnen:")
                        _ih_cols = list(df_ih_raw.columns)
                        c1, c2, c3, c4 = st.columns(4)
                        with c1:
                            ih_cat_col = st.selectbox("Kategorie-Spalte", _ih_cols, key="ih_cat_col")
                        with c2:
                            ih_dat_col = st.selectbox("Datums-Spalte", _ih_cols, key="ih_dat_col")
                        with c3:
                            ih_val_col = st.selectbox("Werte-Spalte", _ih_cols, key="ih_val_col")
                        with c4:
                            ih_val_typ = st.selectbox(
                                "Werttyp", ["Index", "Veränderung z. Vorjahr (%)"], key="ih_val_typ",
                            )
                        ih_date_fmt = st.text_input(
                            "Datumsformat", value="%Y", key="ih_date_fmt",
                            help="z.B. %Y für Jahreswerte ('2024'), %Y-%m für '2024-01', "
                                 "%d.%m.%Y für '01.01.2024'",
                        )
                        if st.button("📥 Importieren", key="ih_import_btn"):
                            df_map = pd.DataFrame({
                                "category": df_ih_raw[ih_cat_col].astype(str).str.strip(),
                                "date": pd.to_datetime(
                                    df_ih_raw[ih_dat_col].astype(str).str.strip(),
                                    format=ih_date_fmt, errors="coerce",
                                ),
                            })
                            df_map["value"] = df_ih_raw[ih_val_col].apply(_destatis_num)
                            n_before = len(df_map)
                            df_map = df_map.dropna(subset=["category", "date", "value"])
                            n_skipped = n_before - len(df_map)

                            if df_map.empty:
                                st.error("Keine gültigen Zeilen gefunden – Spaltenzuordnung/Datumsformat prüfen.")
                            else:
                                if ih_val_typ == "Index":
                                    df_map["index_value"] = df_map["value"]
                                    df_map["yoy_pct"] = None
                                else:
                                    df_map["index_value"] = None
                                    df_map["yoy_pct"] = df_map["value"]
                                n = upsert_inflation_history(
                                    df_map[["category", "date", "index_value", "yoy_pct"]]
                                )
                                st.success(
                                    f"✅ {n} Zeilen importiert/aktualisiert"
                                    + (f" · {n_skipped} übersprungen (ungültig)." if n_skipped else ".")
                                )
                                st.rerun()

        ih_cats_df = list_inflation_history_categories()
        if ih_cats_df.empty:
            st.info("Noch keine historischen Referenzdaten importiert.")
        else:
            _code_to_disp = dict(zip(ih_cats_df["category_code"], ih_cats_df["category"]))
            all_codes = ih_cats_df["category_code"].tolist()

            # ── Ordner-Struktur: Abteilungen (Ebene 2) als "Ordner", darunter
            # optional aufklappbare Unterkategorien (Ebene 3/4/5). Kategorien
            # ohne Gruppierung (generischer Import ohne Hierarchie) erscheinen
            # als eigenständige Einträge auf oberster Ebene.
            top_df   = ih_cats_df[ih_cats_df["level"] == 2]
            flat_df  = ih_cats_df[ih_cats_df["group_code"].isna()]
            child_df = ih_cats_df[ih_cats_df["group_code"].notna() & (ih_cats_df["level"] != 2)]

            top_codes = top_df["category_code"].tolist() + flat_df["category_code"].tolist()

            def _top_disp(c):
                is_division = c in set(top_df["category_code"])
                return f"📁 {_code_to_disp.get(c, c)}" if is_division else _code_to_disp.get(c, c)

            st.caption("Abteilungen auswählen; Unterkategorien optional in den Ordnern darunter aufklappen.")
            sel_codes = st.multiselect(
                "Kategorien anzeigen", options=top_codes,
                default=top_codes[: min(5, len(top_codes))],
                format_func=_top_disp,
                key="ih_sel_cats",
            )

            for grp_code, grp_children in child_df.groupby("group_code"):
                grp_label = grp_children["group_label"].iloc[0] or grp_code
                with st.expander(f"📁 {grp_label} – {len(grp_children)} Unterkategorien"):
                    picked = st.multiselect(
                        grp_label,
                        options=grp_children["category_code"].tolist(),
                        format_func=lambda c: _code_to_disp.get(c, c),
                        key=f"ih_grp_{grp_code}",
                        label_visibility="collapsed",
                    )
                    sel_codes = sel_codes + picked

            if sel_codes:
                hist_df = load_inflation_history(sel_codes).dropna(subset=["yoy_pct"])
                if hist_df.empty:
                    st.info(
                        "Für die gewählten Kategorien liegen keine Veränderungsraten vor "
                        "(z.B. nur ein einzelnes Jahr an Index-Werten importiert)."
                    )
                else:
                    hist_df["category_disp"] = hist_df["category_code"].map(_code_to_disp)
                    fig_ih = px.line(
                        hist_df, x="date", y="yoy_pct", color="category_disp",
                        title="Historische Inflation nach Kategorie (Veränd. z. Vorjahr, %)",
                    )
                    fig_ih.add_hline(y=0, line_color=C["muted"], line_width=1)
                    fig_ih.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                                          yaxis_title="% ggü. Vorjahr", xaxis_title=None,
                                          legend_title=None)
                    st.plotly_chart(fig_ih, config=PLOTLY_CONFIG, width="stretch")

                st.markdown("**Ø jährliche Veränderung – wahlweise als Steigerungssatz oben übernehmen**")
                for code in sel_codes:
                    avg20 = average_yoy_inflation(code, 20)
                    avg10 = average_yoy_inflation(code, 10)
                    rc1, rc2, rc3, rc4, rc5 = st.columns([2, 1, 1, 2, 1])
                    with rc1:
                        st.write(_code_to_disp.get(code, code))
                    with rc2:
                        st.caption(f"Ø 20J: {avg20:.1f} %" if avg20 is not None else "Ø 20J: –")
                    with rc3:
                        st.caption(f"Ø 10J: {avg10:.1f} %" if avg10 is not None else "Ø 10J: –")
                    with rc4:
                        target_grp = st.selectbox(
                            "Ziel-Gruppe", [""] + all_grps, key=f"ih_target_{code}",
                            label_visibility="collapsed",
                        )
                    with rc5:
                        if st.button("Übernehmen", key=f"ih_apply_{code}",
                                     disabled=not target_grp or avg10 is None):
                            upsert_inflation(target_grp, round(avg10, 1))
                            st.success(f"✅ {avg10:.1f} % für Gruppe „{target_grp}“ übernommen.")
                            st.rerun()

                with st.expander("🗑️ Kategorie löschen"):
                    del_code = st.selectbox(
                        "Kategorie", [""] + all_codes,
                        format_func=lambda c: _code_to_disp.get(c, "") if c else "",
                        key="ih_del_cat",
                    )
                    if st.button("Löschen", key="ih_del_btn", disabled=not del_code):
                        delete_inflation_history_category(del_code)
                        st.success("✅ Kategorie gelöscht.")
                        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 2: Vorhersage
# ═══════════════════════════════════════════════════════════════════════════════
with tab_forecast:
    rec_df = load_recurring(active_only=True)
    if rec_df.empty:
        st.info("Noch keine aktiven wiederkehrenden Buchungen vorhanden – "
                "bitte zuerst im Tab „Verwalten“ anlegen.")
    else:

        # ── Steuerleiste ──────────────────────────────────────────────────────────
        with st.expander("⚙️ Vorhersage-Parameter", expanded=True):
            r1c1, r1c2, r1c3 = st.columns([1, 1, 2])
            _today = date.today()
            with r1c1:
                _years = list(range(_today.year - 5, _today.year + 6))
                fc_year = st.selectbox(
                    "Startjahr",
                    options=_years,
                    index=_years.index(_today.year),
                    key="fc_year",
                )
            with r1c2:
                _month_names = [
                    "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
                    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
                ]
                fc_month = st.selectbox(
                    "Startmonat",
                    options=list(range(1, 13)),
                    index=_today.month - 1,
                    format_func=lambda m: _month_names[m - 1],
                    key="fc_month",
                    help="Vorhersage ab diesem Monat starten – in die Zukunft oder Vergangenheit (History-Match).",
                )
            fc_start = date(int(fc_year), int(fc_month), 1)
            with r1c3:
                horizon = st.slider("Zeitraum (Monate)", 3, 60, 24, key="fc_horizon")

            c1, c2, c3 = st.columns(3)
            with c1:
                pct_inc = st.slider("Globaler Aufschlag (%)", -50.0, 100.0, 0.0, 0.5,
                                     key="fc_pct")
            with c2:
                conf    = st.slider("Konfidenzbreite (× σ)", 0.0, 3.0, 1.0, 0.1,
                                     key="fc_conf")
            with c3:
                include_one = st.checkbox("Einmalige Ereignisse einbeziehen",
                                           value=True, key="fc_one")

        # ── What-If: Alternativwerte pro Eintrag ──────────────────────────────────
        with st.expander("🧪 What-If: Alternativwerte für einzelne Buchungen"):
            st.caption("Leere Zellen = Originalwert wird verwendet.")

            st.markdown("**Wiederkehrende Buchungen**")
            whatif = rec_df[["forecast_id", "applicant", "group", "category", "amount"]].copy()
            whatif.insert(0, "aktiv", True)
            whatif["alternativ"] = pd.NA
            whatif_edit = st.data_editor(
                whatif, hide_index=True,
                disabled=["forecast_id", "applicant", "group", "category", "amount"],
                column_config={
                    "aktiv":       st.column_config.CheckboxColumn("Aktiv"),
                    "forecast_id": None,
                    "applicant":   st.column_config.TextColumn(col_app.lab),
                    "group":       st.column_config.TextColumn(col_grp.lab),
                    "category":    st.column_config.TextColumn(col_cat.lab),
                    "amount":      st.column_config.NumberColumn("Original €", format="%.2f €"),
                    "alternativ":  st.column_config.NumberColumn("Alternativ €", format="%.2f"),
                },
                key="whatif_editor", width="stretch",
            )
            overrides = {
                int(r["forecast_id"]): float(r["alternativ"])
                for _, r in whatif_edit.iterrows() if pd.notna(r["alternativ"])
            }
            excluded_ids = {
                int(r["forecast_id"])
                for _, r in whatif_edit.iterrows() if not r["aktiv"]
            }

            one_df_wi = load_oneoff()
            if not one_df_wi.empty and include_one:
                st.markdown("**Einmalzahlungen**")
                whatif_oo = one_df_wi[["oneoff_id", "applicant", "group", "category",
                                        "event_date", "amount"]].copy()
                whatif_oo.insert(0, "aktiv", True)
                whatif_oo["alternativ"] = pd.NA
                whatif_oo_edit = st.data_editor(
                    whatif_oo, hide_index=True,
                    disabled=["oneoff_id", "applicant", "group", "category", "event_date", "amount"],
                    column_config={
                        "aktiv":      st.column_config.CheckboxColumn("Aktiv"),
                        "oneoff_id":  None,
                        "applicant":  st.column_config.TextColumn(col_app.lab),
                        "group":      st.column_config.TextColumn(col_grp.lab),
                        "category":   st.column_config.TextColumn(col_cat.lab),
                        "event_date": st.column_config.DateColumn(col_oo_dat.lab, format="DD.MM.YYYY"),
                        "amount":     st.column_config.NumberColumn("Original €", format="%.2f €"),
                        "alternativ": st.column_config.NumberColumn("Alternativ €", format="%.2f"),
                    },
                    key="whatif_oo_editor", width="stretch",
                )
                oneoff_overrides = {
                    int(r["oneoff_id"]): float(r["alternativ"])
                    for _, r in whatif_oo_edit.iterrows() if pd.notna(r["alternativ"])
                }
                excluded_oneoff_ids = {
                    int(r["oneoff_id"])
                    for _, r in whatif_oo_edit.iterrows() if not r["aktiv"]
                }
            else:
                oneoff_overrides    = {}
                excluded_oneoff_ids = set()

        # ── Inflations-Map laden ──────────────────────────────────────────────────
        infl_df = load_inflation()
        inflation_map = dict(zip(infl_df["group"], infl_df["annual_pct"])) if not infl_df.empty else {}

        # ── Prognose berechnen ────────────────────────────────────────────────────
        fc = compute_forecast(
            horizon_months=int(horizon),
            overrides=overrides,
            oneoff_overrides=oneoff_overrides,
            excluded_ids=excluded_ids,
            excluded_oneoff_ids=excluded_oneoff_ids,
            pct_increase=float(pct_inc),
            confidence=float(conf),
            inflation_map=inflation_map,
            include_oneoff=include_one,
            forecast_start=fc_start,
        )
        monthly = fc["monthly"]
        events  = fc["events"]
        bal_start = fc["balances_start"]

        # ── KPIs ──────────────────────────────────────────────────────────────────
        if not monthly.empty:
            end_saldo   = monthly["saldo"].iloc[-1]
            total_net   = monthly["net"].sum()
            min_saldo   = monthly["saldo_lower"].min()
            avg_net     = monthly["net"].mean()
            avg_income  = monthly["income"].mean()
            avg_expense = monthly["expense"].mean()

            _start_lbl = "heute" if fc_start == date.today().replace(day=1) else fc_start.strftime("%m.%Y")
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1:
                st.metric(f"Startsaldo ({_start_lbl})", f"{sum(bal_start.values()):,.0f} €", border=True)
            with c2:
                color = "green" if end_saldo >= sum(bal_start.values()) else "red"
                st.metric(f"Endsaldo nach {int(horizon)} M", f":{color}[{end_saldo:,.0f} €]",
                           f"{total_net:+,.0f} €",
                           delta_color="normal", delta_arrow="off", border=True)
            with c3:
                st.metric("Ø monatl. Einnahmen", f":green[{avg_income:,.0f} €]",
                           delta_arrow="off", border=True)
            with c4:
                st.metric("Ø monatl. Ausgaben", f":red[{abs(avg_expense):,.0f} €]",
                           delta_arrow="off", border=True)
            with c5:
                color = "green" if avg_net >= 0 else "red"
                st.metric("Ø monatl. Saldo", f":{color}[{avg_net:+,.0f} €]",
                           delta_arrow="off", border=True)
            with c6:
                color = "green" if min_saldo >= 0 else "red"
                st.metric("Min. Saldo (untere Grenze)", f":{color}[{min_saldo:,.0f} €]",
                           delta_arrow="off", border=True)

        # ── Saldoverlauf mit Konfidenzband ────────────────────────────────────────
        st.divider()
        fig = go.Figure()
        # Konfidenzband
        if conf > 0:
            fig.add_scatter(
                x=monthly["year_month"], y=monthly["saldo_upper"],
                mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip",
            )
            fig.add_scatter(
                x=monthly["year_month"], y=monthly["saldo_lower"],
                mode="lines", line=dict(width=0), fill="tonexty",
                fillcolor="rgba(77,159,255,0.15)", name=f"Konfidenz ±{conf:.1f}σ",
            )
        fig.add_scatter(
            x=monthly["year_month"], y=monthly["saldo"],
            mode="lines+markers", name="Erwarteter Saldo",
            line=dict(color=C["blue"], width=2),
            hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>",
        )
        fig.add_hline(y=0, line_color=C["muted"], line_width=1.5, line_dash="dot")
        fig.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT, title="Saldoverlauf",
                           legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1))
        fig.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
        fig.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

        # ── Monatliche Einnahmen/Ausgaben/Net ─────────────────────────────────────
        c1, c2 = st.columns([3, 2], gap="medium")
        with c1:
            fig_ie = go.Figure()
            fig_ie.add_bar(x=monthly["year_month"], y=monthly["income"],
                            name="Einnahmen", marker_color=C["green"],
                            hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>")
            fig_ie.add_bar(x=monthly["year_month"], y=monthly["expense"],
                            name="Ausgaben", marker_color=C["red"],
                            hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>")
            fig_ie.add_scatter(x=monthly["year_month"], y=monthly["net"],
                                mode="lines+markers", name="Saldo",
                                line=dict(color=C["amber"], width=2),
                                hovertemplate="<b>%{x}</b><br>Saldo: %{y:,.0f} €<extra></extra>")
            fig_ie.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT, barmode="relative",
                                  title="Monatliche Einnahmen / Ausgaben",
                                  legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1))
            fig_ie.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
            fig_ie.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
            st.plotly_chart(fig_ie, width="stretch", config=PLOTLY_CONFIG)

        with c2:
            # Drilldown: gestapelte Ausgaben pro Gruppe
            if not events.empty:
                exp_events = events[events["amount"] < 0].copy()
                if not exp_events.empty:
                    exp_events["year_month"] = exp_events["date"].dt.strftime("%Y-%m")
                    pivot = (exp_events.assign(amount=lambda d: d["amount"].abs())
                             .groupby(["year_month", "group"])["amount"]
                             .sum().reset_index())
                    fig_st = px.bar(
                        pivot, x="year_month", y="amount", color="group",
                        color_discrete_sequence=[C["red"], C["amber"], C["blue"], C["green"],
                                                  C["purple"], "#F472B6", "#34D399"],
                    )
                    fig_st.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                                          title="Ausgaben pro Gruppe (gestapelt)",
                                          legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=0))
                    fig_st.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
                    fig_st.update_yaxes(showgrid=True, gridcolor=C["border"],
                                         ticksuffix=" €", tickformat=",.0f")
                    fig_st.update_traces(hovertemplate="<b>%{fullData.name}</b><br>%{x}<br>%{y:,.0f} €<extra></extra>")
                    st.plotly_chart(fig_st, width="stretch", config=PLOTLY_CONFIG)

        # ── Ø Einnahmequellen & Ausgaben-Breakdown ───────────────────────────────
        if not events.empty:
            st.divider()
            n_months = len(monthly)
            inc_ev = events[events["amount"] > 0]
            exp_ev = events[events["amount"] < 0].assign(amount=lambda d: d["amount"].abs())

            c_inc, c_exp = st.columns(2, gap="medium")

            with c_inc:
                if not inc_ev.empty:
                    src = (
                        inc_ev.groupby("applicant")["amount"].sum()
                        .div(n_months).reset_index()
                        .sort_values("amount")
                    )
                    fig_src = go.Figure(go.Bar(
                        x=src["amount"], y=src["applicant"], orientation="h",
                        marker_color=C["green"],
                        text=src["amount"].apply(lambda v: f"{v:,.0f} €"),
                        textposition="outside",
                        hovertemplate="<b>%{y}</b><br>Ø %{x:,.0f} €/M<extra></extra>",
                    ))
                    fig_src.update_layout(
                        **PLOTLY_THEME,
                        height=CHART_HEIGHT,
                        title="Ø Einnahmequellen pro Monat",
                        showlegend=False,
                    )
                    fig_src.update_xaxes(showgrid=True, gridcolor=C["border"],
                                         ticksuffix=" €", tickformat=",.0f")
                    fig_src.update_yaxes(showgrid=False)
                    st.plotly_chart(fig_src, width="stretch", config=PLOTLY_CONFIG)
                else:
                    st.info("Keine Einnahmen in der Prognose.")

            with c_exp:
                if not exp_ev.empty and "category" in exp_ev.columns:
                    grp_cat = (
                        exp_ev.groupby(["group", "category"])["amount"].sum()
                        .div(n_months).reset_index()
                    )
                    fig_exp = px.sunburst(
                        grp_cat, path=["group", "category"], values="amount",
                        color="group",
                        color_discrete_sequence=[C["red"], C["amber"], C["blue"],
                                                  C["green"], C["purple"],
                                                  "#38BDF8", "#F472B6", "#34D399"],
                    )
                    fig_exp.update_traces(
                        textinfo="label+percent parent",
                        hovertemplate=(
                            "<b>%{label}</b><br>"
                            "Ø %{value:,.0f} €/M<br>"
                            "%{percentParent:.1%} der Gruppe<br>"
                            "%{percentRoot:.1%} gesamt<extra></extra>"
                        ),
                    )
                    fig_exp.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                                          title="Ø Ausgaben nach Gruppe & Kategorie pro Monat")
                    st.plotly_chart(fig_exp, width="stretch", config=PLOTLY_CONFIG_RADIAL)
                else:
                    st.info("Keine Ausgaben in der Prognose.")

        # ── Matrix: Kontext × Beziehung ──────────────────────────────────────────
        if not events.empty and {"relation", "context"}.issubset(events.columns):
            st.divider()
            _mx_ev = events[events["amount"] < 0].assign(amount=lambda d: d["amount"].abs())
            if not _mx_ev.empty:
                _n_months = max(len(monthly), 1)
                _grp = _mx_ev.groupby(["context", "relation"])["amount"].sum().reset_index()
                _pivot_sum = _grp.pivot(index="context", columns="relation", values="amount").fillna(0)
                _pivot_avg = _pivot_sum / _n_months

                _cell_text = [
                    [
                        f"Ø {_pivot_avg.values[r][c]:,.0f} €<br>Σ {_pivot_sum.values[r][c]:,.0f} €"
                        if _pivot_sum.values[r][c] > 0 else ""
                        for c in range(_pivot_sum.shape[1])
                    ]
                    for r in range(_pivot_sum.shape[0])
                ]
                _hover_text = [
                    [
                        (
                            f"<b>{_pivot_avg.index[r]} × {_pivot_avg.columns[c]}</b><br>"
                            f"Ø {_pivot_avg.values[r][c]:,.0f} €/M<br>"
                            f"Σ {_pivot_sum.values[r][c]:,.0f} € gesamt"
                        )
                        for c in range(_pivot_sum.shape[1])
                    ]
                    for r in range(_pivot_sum.shape[0])
                ]

                fig_mx = go.Figure(go.Heatmap(
                    z=_pivot_avg.values,
                    x=_pivot_avg.columns.tolist(),
                    y=_pivot_avg.index.tolist(),
                    colorscale=[[0, "rgba(0,0,0,0)"], [0.01, "#1a2a3a"], [1, C["blue"]]],
                    hoverongaps=False,
                    customdata=_hover_text,
                    hovertemplate="%{customdata}<extra></extra>",
                    text=_cell_text,
                    texttemplate="%{text}",
                    showscale=False,
                ))
                fig_mx.update_layout(
                    **PLOTLY_THEME,
                    height=CHART_HEIGHT,
                    title="Ausgaben: Kontext × Beziehung",
                    xaxis=dict(title="Beziehung", side="bottom"),
                    yaxis=dict(title="Kontext", autorange="reversed"),
                )
                st.plotly_chart(fig_mx, width="stretch", config=PLOTLY_CONFIG)
            else:
                st.info("Keine Ausgaben für die Matrix verfügbar.")

        # ── Tabelle ───────────────────────────────────────────────────────────────
        st.divider()
        with st.expander("📊 Monatliche Prognose", expanded=False):
            disp = monthly[["year_month", "income", "expense", "net",
                            "saldo_lower", "saldo", "saldo_upper"]].copy()
            st.dataframe(
                disp.style
                    .map(colour_amount, subset=["net", "saldo", "saldo_lower", "saldo_upper"])
                    .format({c: "{:,.2f} €" for c in disp.columns if c != "year_month"}),
                hide_index=True, height=420,
                column_config={
                    "year_month":  "Monat",
                    "income":      "Einnahmen",
                    "expense":     "Ausgaben",
                    "net":         "Saldo",
                    "saldo_lower": "Saldo unten",
                    "saldo":       "Saldo erwartet",
                    "saldo_upper": "Saldo oben",
                },
                width="stretch",
            )

        # ── Buchungsliste mit tagesgenauem Saldenverlauf ─────────────────────────
        with st.expander("📋 Monatliche Buchungen", expanded=False):
            if not events.empty:
                start_bal = sum(bal_start.values())
                ev_sorted = events.sort_values("date").copy()
                ev_sorted["saldo"]       = start_bal + ev_sorted["amount"].cumsum()
                ev_sorted["saldo_lower"] = start_bal + ev_sorted["lower"].cumsum()
                ev_sorted["saldo_upper"] = start_bal + ev_sorted["upper"].cumsum()

                disp_ev = ev_sorted[["date", "applicant", "group", "category",
                                     "context", "relation", "amount", "saldo"]].copy()
                st.dataframe(
                    disp_ev.style
                        .map(colour_amount, subset=["amount", "saldo"])
                        .format({
                            "amount": "{:,.2f} €",
                            "saldo":  "{:,.2f} €",
                            "date":   lambda d: d.strftime("%d.%m.%Y") if pd.notna(d) else "",
                        }),
                    hide_index=True,
                    height=420,
                    width="stretch",
                    column_config={
                        "date":      "Datum",
                        "applicant": col_app.lab,
                        "group":     col_grp.lab,
                        "category":  col_cat.lab,
                        "context":   col_ctx.lab,
                        "relation":  col_rel.lab,
                        "amount":    col_amt.lab,
                        "saldo":     "Saldo",
                    },
                )
            else:
                st.info("Keine Buchungen in der Prognose.")

        # ── Export ────────────────────────────────────────────────────────────────
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "📋 Monatsübersicht als CSV",
                data=monthly.to_csv(index=False).encode(),
                file_name=f"prognose_monthly_{date.today()}.csv",
                mime="text/csv", width="stretch",
            )
        with c2:
            if not events.empty:
                st.download_button(
                    "📋 Alle Buchungen als CSV",
                    data=events.to_csv(index=False).encode(),
                    file_name=f"prognose_events_{date.today()}.csv",
                    mime="text/csv", width="stretch",
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 3: Warnungen
# ═══════════════════════════════════════════════════════════════════════════════
with tab_warn:
    st.subheader("Liquiditätswarnungen")
    st.caption(
        "Zeigt Monate, in denen der untere Rand des Konfidenzbands unter den "
        "Schwellwert fällt – ein Frühwarnsystem für Engpässe."
    )

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        warn_threshold = st.number_input("Schwellwert (€)", value=1000.0, step=500.0)
    with c2:
        warn_horizon   = st.slider("Zeitraum (Monate)", 3, 60, 24, key="warn_horizon")
    with c3:
        warn_conf      = st.slider("Konfidenz (× σ)", 0.0, 3.0, 1.0, 0.1, key="warn_conf")

    _warn_infl = load_inflation()
    infl_map_warn = dict(zip(_warn_infl["group"], _warn_infl["annual_pct"])) if not _warn_infl.empty else {}

    _warn_key = (int(warn_horizon), float(warn_conf), float(warn_threshold))
    if st.session_state.get("_warn_fc_key") != _warn_key:
        st.session_state["_warn_fc"] = compute_forecast(
            horizon_months=int(warn_horizon),
            confidence=float(warn_conf),
            inflation_map=infl_map_warn,
        )
        st.session_state["_warn_fc_key"] = _warn_key
    fc_warn = st.session_state["_warn_fc"]
    warn_df = liquidity_warnings(fc_warn["monthly"], float(warn_threshold))

    if warn_df.empty:
        st.success(f"✅ Keine Warnungen: Der prognostizierte Saldo bleibt in allen "
                    f"{int(warn_horizon)} Monaten über {warn_threshold:,.0f} €.")
    else:
        first = warn_df.iloc[0]
        st.error(f"⚠️ {len(warn_df)} kritische(r) Monat(e) erkannt – "
                  f"erstmals **{first['year_month']}** mit Saldo unten **{first['saldo_lower']:,.0f} €**.")
        st.dataframe(
            warn_df[["year_month", "saldo_lower", "saldo", "saldo_upper", "net"]]
                .style.map(colour_amount, subset=["saldo_lower", "saldo", "saldo_upper", "net"])
                .format({c: "{:,.2f} €" for c in ["saldo_lower", "saldo", "saldo_upper", "net"]}),
            hide_index=True, width="stretch",
            column_config={
                "year_month":  "Monat",
                "saldo_lower": "Saldo unten",
                "saldo":       "Saldo erwartet",
                "saldo_upper": "Saldo oben",
                "net":         "Monatssaldo",
            },
        )

        # Visualisierung mit Schwellwert-Linie
        fig_w = go.Figure()
        fig_w.add_scatter(x=fc_warn["monthly"]["year_month"], y=fc_warn["monthly"]["saldo_lower"],
                          mode="lines", name="Saldo unten", line=dict(color=C["red"], width=1, dash="dot"))
        fig_w.add_scatter(x=fc_warn["monthly"]["year_month"], y=fc_warn["monthly"]["saldo"],
                          mode="lines+markers", name="Saldo erwartet", line=dict(color=C["blue"], width=2))
        fig_w.add_hline(y=float(warn_threshold), line_color=C["amber"], line_dash="dash",
                         annotation_text=f"Schwelle {warn_threshold:,.0f} €", annotation_position="top right")
        fig_w.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                             legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1))
        fig_w.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
        fig_w.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
        st.plotly_chart(fig_w, width="stretch", config=PLOTLY_CONFIG)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 4: Prognose vs. Ist
# ═══════════════════════════════════════════════════════════════════════════════
with tab_compare:
    st.subheader("Rückblick: Prognose vs. tatsächliche Buchungen")
    st.caption(
        "Vergleicht für vergangene Monate, was die aktuelle Konfiguration "
        "vorhergesagt hätte gegen die tatsächlichen Buchungen. Hilft die "
        "Genauigkeit der Forecast-Setup einzuschätzen."
    )
    c_sl, c_cb = st.columns([3, 1])
    with c_sl:
        months_back = st.slider("Wie viele Monate rückblickend?", 1, 24, 6, key="cmp_months")
    with c_cb:
        st.markdown("<br>", unsafe_allow_html=True)
        excl_special = st.checkbox("Spezial ausblenden", key="cmp_excl_special",
                                   help="Ist-Buchungen mit ‚spezial = Ja' aus dem Vergleich herausnehmen.")
    cmp_df = forecast_vs_actual(int(months_back), exclude_special=excl_special)

    if cmp_df.empty:
        st.info("Keine Daten für den Vergleich verfügbar.")
    else:
        # KPIs
        total_pred = cmp_df["prognose"].sum()
        total_act  = cmp_df["ist"].sum()
        total_dev  = cmp_df["abweichung"].sum()
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Σ Prognose", f"{total_pred:,.0f} €", border=True)
        with c2:
            st.metric("Σ Ist", f"{total_act:,.0f} €", border=True)
        with c3:
            color = "green" if total_dev >= 0 else "red"
            st.metric("Σ Abweichung", f":{color}[{total_dev:+,.0f} €]",
                       delta_arrow="off", border=True)

        fig_c = go.Figure()
        fig_c.add_bar(x=cmp_df["year_month"], y=cmp_df["prognose"],
                       name="Prognose", marker_color=C["blue"], opacity=0.7,
                       hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>")
        fig_c.add_bar(x=cmp_df["year_month"], y=cmp_df["ist"],
                       name="Ist", marker_color=C["green"], opacity=0.7,
                       hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>")
        fig_c.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT, barmode="group",
                             legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1))
        fig_c.update_xaxes(showgrid=False, tickangle=45)
        fig_c.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
        st.plotly_chart(fig_c, width="stretch", config=PLOTLY_CONFIG)

        st.dataframe(
            cmp_df.style.map(colour_amount, subset=["prognose", "ist", "abweichung"])
                       .format({c: "{:,.2f} €" for c in ["prognose", "ist", "abweichung"]}),
            hide_index=True, width="stretch",
            column_config={
                "year_month": "Monat",
                "prognose":   "Prognose",
                "ist":        "Ist",
                "abweichung": "Δ (Ist − Prognose)",
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 5: Szenarien
# ═══════════════════════════════════════════════════════════════════════════════
with tab_scenarios:
    st.subheader("Szenarien speichern und vergleichen")
    st.caption(
        "Speichert die aktuell im Tab „Vorhersage“ eingestellten Parameter "
        "(Aufschlag, Konfidenz, Zeitraum) unter einem Namen. Verschiedene "
        "Szenarien können nebeneinander verglichen werden."
    )

    c1, c2 = st.columns([2, 1])
    with c1:
        sc_name = st.text_input("Szenario-Name", placeholder="z.B. „Gehaltserhöhung +5%“")
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Aktuelle Parameter speichern", width="stretch"):
            if not sc_name.strip():
                st.error("Bitte einen Namen vergeben.")
            elif is_reserved_scenario_name(sc_name):
                st.error("Dieser Name ist intern reserviert. Bitte einen anderen wählen.")
            else:
                save_scenario(sc_name.strip(), {
                    "horizon":      int(st.session_state.get("fc_horizon", 24)),
                    "pct_increase": float(st.session_state.get("fc_pct", 0.0)),
                    "confidence":   float(st.session_state.get("fc_conf", 1.0)),
                    "include_one":  bool(st.session_state.get("fc_one", True)),
                })
                st.success(f"✅ Szenario „{sc_name}“ gespeichert.")
                st.rerun()

    saved = list_scenarios()
    if not saved:
        st.info("Noch keine Szenarien gespeichert.")
    else:
        st.write(f"**{len(saved)} Szenarien gespeichert:**")
        compare_sel = st.multiselect(
            "Szenarien zum Vergleich auswählen", saved, default=saved[:3],
        )

        if compare_sel:
            _sc_infl = load_inflation()
            infl_map = dict(zip(_sc_infl["group"], _sc_infl["annual_pct"])) if not _sc_infl.empty else {}
            fig_cmp = go.Figure()
            cmp_rows = []
            for name in compare_sel:
                p = load_scenario(name)
                if not p or "horizon" not in p:
                    st.warning(f"Szenario „{name}“ enthält keine Prognose-Parameter – übersprungen.")
                    continue
                fc_s = compute_forecast(
                    horizon_months=int(p["horizon"]),
                    pct_increase=float(p.get("pct_increase", 0.0)),
                    confidence=float(p.get("confidence", 1.0)),
                    inflation_map=infl_map,
                    include_oneoff=bool(p.get("include_one", True)),
                )
                m = fc_s["monthly"]
                fig_cmp.add_scatter(
                    x=m["year_month"], y=m["saldo"], mode="lines+markers", name=name,
                    hovertemplate=f"<b>{name}</b><br>%{{x}}<br>%{{y:,.0f}} €<extra></extra>",
                )
                cmp_rows.append({
                    "Szenario":         name,
                    "Aufschlag":        f"{float(p.get('pct_increase', 0.0)):+.1f} %",
                    "Konfidenz":        f"{float(p.get('confidence', 1.0)):.1f}σ",
                    "Zeitraum":         f"{int(p['horizon'])} M",
                    "Endsaldo":         m["saldo"].iloc[-1] if not m.empty else 0,
                    "Min. Saldo unten": m["saldo_lower"].min() if not m.empty else 0,
                })
            fig_cmp.add_hline(y=0, line_color=C["border"], line_width=1)
            fig_cmp.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                                   title="Saldoverlauf je Szenario",
                                   legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1))
            fig_cmp.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
            fig_cmp.update_yaxes(showgrid=True, gridcolor=C["border"],
                                  ticksuffix=" €", tickformat=",.0f")
            st.plotly_chart(fig_cmp, width="stretch", config=PLOTLY_CONFIG)

            cmp_summary = pd.DataFrame(cmp_rows)
            st.dataframe(
                cmp_summary.style.format({"Endsaldo": "{:,.0f} €", "Min. Saldo unten": "{:,.0f} €"}),
                hide_index=True, width="stretch",
            )

        # Löschen
        with st.expander("🗑️ Szenario löschen"):
            del_sc = st.selectbox("Szenario", options=[None] + saved,
                                   format_func=lambda s: "—" if s is None else s)
            if st.button("Löschen", disabled=del_sc is None, type="primary"):
                delete_scenario(del_sc)
                st.success(f"✅ „{del_sc}“ gelöscht.")
                st.rerun()
