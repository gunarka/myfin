"""
app_assign.py
Transaktionen kategorisieren und in der Datenbank aktualisieren.
Ermöglicht gefilterte Ansicht und direkte In-Place-Bearbeitung per data_editor.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.express as px
import streamlit as st
from datetime import date

from app_functions import (
    require_master_password,
    list_saved_users,
    load_categories,
    save_category,
    get_or_create_category_id,
    build_select,
    safe_table_name,
    colour_amount,
    get_config,
    inc_filter,
    exc_filter,
    MONTH_NAMES,
    render_category_filters,
    con,
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app,
    col_loc, col_dat, col_mon, col_yea,
    col_inf, col_add, col_new, col_rid, col_spc, col_note, col_catid,
    COL_LABELS,
)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("🔖 Zuordnen")


# ── Konto-Auswahl ─────────────────────────────────────────────────────────────
accounts  = list_saved_users()
_all_keys = accounts["KEY"].tolist()
_giro     = accounts[accounts["Konto"].str.lower() == "giro"]
_other    = accounts[accounts["Konto"].str.lower() != "giro"]
_fmt      = lambda k: accounts.set_index("KEY").loc[k, "LABEL"]

if not _all_keys:
    st.info("Noch keine Konten konfiguriert. Bitte unter ⚙️ Administrieren anlegen.")
    st.stop()

if "sel_assign" not in st.session_state or st.session_state["sel_assign"] not in _all_keys:
    st.session_state["sel_assign"] = _all_keys[0]

def _sel_assign(k: str) -> None:
    st.session_state["sel_assign"] = k

with st.expander("🏦 Konto", expanded=True):
    c1, c2 = st.columns(2)
    for _col, _subset in [(c1, _giro), (c2, _other)]:
        with _col:
            for _, _row in _subset.iterrows():
                st.button(
                    _fmt(_row["KEY"]),
                    key=f"btn_asgn_{_row['KEY']}",
                    width="stretch",
                    type="primary" if st.session_state["sel_assign"] == _row["KEY"] else "secondary",
                    on_click=_sel_assign,
                    args=(_row["KEY"],),
                )

selected = st.session_state["sel_assign"]
raw_iban = accounts[accounts["KEY"] == selected]["IBAN"].iloc[0]

try:
    var_acc = safe_table_name(raw_iban)
except ValueError as e:
    st.error(f"Ungültiges Konto: {e}")
    st.stop()


# ── Änderungen in die DB schreiben ────────────────────────────────────────────
# WICHTIG: Wir vergleichen NICHT df_orig.compare(df_edited) über das gesamte
# Grid. Grund: Streamlit validiert bei jedem Rerun ALLE SelectboxColumn-Zellen
# (hier: grp_cat) gegen die aktuelle options-Liste. Ist eine Zeile (z.B. durch
# Timing zwischen SQL-Reload und Widget-State) nicht exakt in all_combos
# enthalten, wird die Zelle von Streamlit intern auf None zurückgesetzt –
# unabhängig davon, ob der Nutzer sie überhaupt angefasst hat. Ein Vergleich
# der kompletten DataFrames erkennt diese Zeilen dann fälschlich als
# "geändert" und überschreibt gültige Gruppe/Kategorie-Werte mit NULL,
# sobald der Nutzer IRGENDEINE andere Zeile bearbeitet.
#
# Fix: Nur die von Streamlit selbst protokollierten Edits verwenden
# (st.session_state[editor_key]["edited_rows"]). Dieses Dict enthält
# ausschließlich Zellen, die der Nutzer tatsächlich verändert hat.

def write_changes(df_display: pd.DataFrame, editor_key: str, var_acc: str) -> None:
    """
    Schreibt nur tatsächlich vom Nutzer bearbeitete Zellen in die DB.
    SICHERHEIT: Spaltennamen kommen aus bekannter col-Liste, nicht aus User-Input;
    row_id wird als Parameter gebunden (kein String-Interpolation von Werten).
    """
    edits = st.session_state.get(editor_key, {}).get("edited_rows", {})
    if not edits:
        st.info("Keine Änderungen.")
        return

    n_rows = 0
    for pos, changed in edits.items():
        row_id = df_display.iloc[int(pos)][col_rid.col]
        fields: dict = {}

        if "grp_cat" in changed:
            val = changed["grp_cat"] or ""
            g, c = val.split(" · ", 1) if " · " in val else (None, None)
            fields[col_catid.col] = get_or_create_category_id(g or None, c or None)

        for c_name in (col_ctx.col, col_rel.col, col_note.col, col_new.col, col_spc.col):
            if c_name in changed:
                fields[c_name] = changed[c_name]

        if not fields:
            continue

        set_clause = ", ".join(f'"{k}" = ?' for k in fields)
        con.execute(
            f'UPDATE "{var_acc}" SET {set_clause} WHERE "{col_rid.col}" = ?',
            list(fields.values()) + [int(row_id)],
        )
        n_rows += 1

    st.success(f"✅ {n_rows} Zeile(n) gespeichert.")


# ── Daten laden ───────────────────────────────────────────────────────────────
columns = [
    col_rid, col_yea, col_mon, col_app, col_loc,
    col_amt, col_inf, col_add, col_grp, col_cat,
    col_ctx, col_rel, col_new, col_spc, col_dat, col_note,
]
df = con.sql(build_select([c.col for c in columns], var_acc)).df()
st.session_state.df = df

# Fehlende Kategorien auffüllen
for c in [col_grp, col_cat, col_ctx, col_rel]:
    df[c.col] = df[c.col].fillna("")

df[col_new.col] = df[col_new.col].fillna(False).astype(bool)
df[col_spc.col] = df[col_spc.col].fillna(False).astype(bool)

# ── Kategorien aus dedizierter Tabelle laden ──────────────────────────────────
# grps/cats kommen aus der categories-Tabelle, nicht aus den Transaktionsdaten.
# ctxs/rels weiterhin aus den Transaktionsdaten (keine eigene Tabelle).
cat_df = load_categories()
all_grps   = sorted(cat_df[col_grp.col].unique().tolist(), key=str.lower)
all_cats   = sorted(cat_df[col_cat.col].unique().tolist(), key=str.lower)
all_combos = sorted(
    [f"{r[col_grp.col]} · {r[col_cat.col]}" for _, r in cat_df.iterrows()],
    key=str.lower,
)

# Kombinationen, die in den Buchungsdaten vorkommen, aber (noch) nicht in der
# categories-Tabelle stehen (z.B. durch automatische Zuordnung beim Import),
# ebenfalls als Editor-Optionen zulassen. Sonst kann die SelectboxColumn den
# Wert bei einem Rerun nicht darstellen, verwirft ihn intern, und die Zeile
# wird beim Speichern fälschlich als "geändert" (auf leer) erkannt.
_existing_grps   = set(df[col_grp.col]) - {""}
_existing_cats   = set(df[col_cat.col]) - {""}
_existing_combos = {
    f"{g} · {c}" for g, c in zip(df[col_grp.col], df[col_cat.col]) if g and c
}
all_grps   = sorted(set(all_grps)   | _existing_grps,   key=str.lower)
all_cats   = sorted(set(all_cats)   | _existing_cats,   key=str.lower)
all_combos = sorted(set(all_combos) | _existing_combos, key=str.lower)

ctxs     = sorted(df[col_ctx.col].unique(), key=str.lower)
yeas     = sorted(df[col_yea.col].unique())
rels     = sorted(df[col_rel.col].unique(), key=str.lower)

# Session-State für ctxs/rels (können noch ergänzt werden)
for key, val in [("ctxs", ctxs), ("rels", rels)]:
    st.session_state.setdefault(key, val)


df["year_month"] = df[col_yea.col].astype(str) + "-" + df[col_mon.col].astype(str).str.zfill(2)
_today_ym = date.today().strftime("%Y-%m")
_ym_opts = (
    [d.strftime("%Y-%m") for d in pd.date_range(f"{df['year_month'].min()}-01", f"{_today_ym}-01", freq="MS")]
    if yeas else []
)


# ── Filter-UI (gemeinsamer Helper mit app_dashboard.py) ──────────────────────
# Gruppe und Kategorie schränken sich gegenseitig ein:
# Gewählte Gruppen → nur passende Kategorien sichtbar (und umgekehrt).
# Beide leer → alle Optionen verfügbar.
def _render_ym_slider():
    if not _ym_opts:
        return (None, None)
    _today_dt = pd.Timestamp(date.today())
    _def_e = min(_today_dt.strftime("%Y-%m"), _ym_opts[-1])
    _def_s = max((_today_dt - pd.DateOffset(months=11)).strftime("%Y-%m"), _ym_opts[0])
    return st.select_slider(
        "Zeitraum",
        options=_ym_opts,
        value=(_def_s, _def_e),
        format_func=lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}",
        key="sl_ym",
    )

_f = render_category_filters(
    cat_df, all_grps, all_cats, ctxs, rels,
    search_cols=[col_app.col, col_inf.col, col_add.col],
    def_flr_new=True,
    render_extra=_render_ym_slider,
)
_ym_range           = _f["extra"]
flr_grp, exc_grp    = _f["flr_grp"], _f["exc_grp"]
flr_cat, exc_cat    = _f["flr_cat"], _f["exc_cat"]
flr_new, exc_new    = _f["flr_new"], _f["exc_new"]
flr_cnt, exc_cnt    = _f["flr_cnt"], _f["exc_cnt"]
flr_rel, exc_rel    = _f["flr_rel"], _f["exc_rel"]
flr_spc, exc_spc    = _f["flr_spc"], _f["exc_spc"]
src_col1, src_txt1  = _f["src_col1"], _f["src_txt1"]
src_col2, src_txt2  = _f["src_col2"], _f["src_txt2"]

# ── Filter anwenden ───────────────────────────────────────────────────────────
_ym_mask = (
    (df["year_month"] >= _ym_range[0]) & (df["year_month"] <= _ym_range[1])
    if _ym_range[0] is not None else pd.Series(True, index=df.index)
)
df_flr = df[
    inc_filter(df[col_grp.col], flr_grp) & exc_filter(df[col_grp.col], exc_grp) &
    inc_filter(df[col_new.col], flr_new) & exc_filter(df[col_new.col], exc_new) &
    inc_filter(df[col_cat.col], flr_cat) & exc_filter(df[col_cat.col], exc_cat) &
    _ym_mask &
    inc_filter(df[col_ctx.col], flr_cnt) & exc_filter(df[col_ctx.col], exc_cnt) &
    inc_filter(df[col_rel.col], flr_rel) & exc_filter(df[col_rel.col], exc_rel) &
    inc_filter(df[col_spc.col], flr_spc) & exc_filter(df[col_spc.col], exc_spc) &
    df[src_col1].fillna("").str.contains(src_txt1, case=False, regex=False) &
    df[src_col2].fillna("").str.contains(src_txt2, case=False, regex=False)
]

# ── Balkendiagramm ────────────────────────────────────────────────────────────
with st.expander("📊 Diagramm"):
    fig = px.bar(
        df_flr, y=col_amt.col, x=col_grp.col, color=col_cat.col,
        orientation="v", hover_data=[col_app.col, col_amt.col],
    )
    st.plotly_chart(fig, width='stretch')

# ── Neue Gruppe/Kategorie anlegen ─────────────────────────────────────────────
with st.expander("➕ Neue Kategorie anlegen"):
    with st.form("form_new_cat", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            new_grp = st.text_input(f"Neue {col_grp.lab}", key="t_grp")
        with c2:
            new_cat = st.text_input(f"Neue {col_cat.lab}", key="t_cat")
        with c3:
            st.markdown("<br>", unsafe_allow_html=True)
            submitted = st.form_submit_button("💾 Speichern")
        if submitted:
            if not new_grp.strip() or not new_cat.strip():
                st.error("Gruppe und Kategorie sind Pflichtfelder.")
            else:
                try:
                    inserted = save_category(new_grp, new_cat)
                    if inserted:
                        st.success(f"✅ '{new_grp} · {new_cat}' gespeichert.")
                        st.rerun()
                    else:
                        st.warning("Kombination bereits vorhanden.")
                except ValueError as e:
                    st.error(str(e))

with st.expander("➕ Kontext / Beziehung ergänzen"):
    c1, c2 = st.columns(2)
    with c1:
        new_ctx = st.text_input(f"Neuer {col_ctx.lab}:", key="t_ctxs")
        if st.button("Hinzufügen", key="b_ctxs") and new_ctx:
            if new_ctx not in st.session_state["ctxs"]:
                st.session_state["ctxs"].append(new_ctx)
    with c2:
        new_rel = st.text_input(f"Neue {col_rel.lab}:", key="t_rels")
        if st.button("Hinzufügen", key="b_rels") and new_rel:
            if new_rel not in st.session_state["rels"]:
                st.session_state["rels"].append(new_rel)

# ── Editierbarer Datatable + Kategorien-Referenz ─────────────────────────────
def _toggle_ref() -> None:
    st.session_state["show_cat_ref"] = not st.session_state.get("show_cat_ref", True)

_show_ref = st.session_state.get("show_cat_ref", False)
_, _col_btn = st.columns([3, 1])
with _col_btn:
    st.button(
        "📋 Referenz ausblenden" if _show_ref else "📋 Referenz einblenden",
        on_click=_toggle_ref,
        key="btn_toggle_ref",
        width="stretch",
    )

if _show_ref:
    col_editor, col_ref = st.columns([3, 1])
else:
    col_editor = st.container()

with col_editor:
    df_display = df_flr.copy()
    _grp = df_display[col_grp.col].fillna("").astype(str)
    _cat = df_display[col_cat.col].fillna("").astype(str)
    df_display["grp_cat"] = [
        f"{g} · {c}" if g and c else None
        for g, c in zip(_grp, _cat)
    ]
    _cols = list(df_display.columns)
    _cols.remove("grp_cat")
    _cols.insert(_cols.index(col_grp.col), "grp_cat")
    df_display = df_display[_cols].reset_index(drop=True)

    st.data_editor(
        df_display.style
            .map(colour_amount, subset=[col_amt.col])
            .format({col_amt.col: "{:,.2f} €"}),
        key="main_editor",
        hide_index=True,
        num_rows="fixed",
        height=642,
        disabled=[c.col for c in [col_rid, col_yea, col_mon, col_dat, col_app, col_loc, col_amt, col_inf, col_add]],
        column_config=get_config(columns) | {
            col_rid.col: None,
            col_yea.col: None,
            col_mon.col: None,
            "year_month": None,
            col_grp.col: None,
            col_cat.col: None,
            "grp_cat": st.column_config.SelectboxColumn(
                options=all_combos, label=f"{col_grp.lab} · {col_cat.lab}"
            ),
            col_ctx.col: st.column_config.SelectboxColumn(options=st.session_state.ctxs, label=col_ctx.lab),
            col_rel.col: st.column_config.SelectboxColumn(options=st.session_state.rels, label=col_rel.lab),
            col_note.col: st.column_config.TextColumn(label=col_note.lab),
            col_new.col: st.column_config.CheckboxColumn(label=col_new.lab),
            col_spc.col: st.column_config.CheckboxColumn(label=col_spc.lab),
        },
    )


if _show_ref:
    with col_ref:
        s1, s2 = st.columns(2)
        q_grp = s1.text_input("Gruppe", key="ref_q_grp", placeholder="🔍", label_visibility="collapsed")
        q_cat = s2.text_input("Kategorie", key="ref_q_cat", placeholder="🔍", label_visibility="collapsed")
        ref_df = (
            cat_df[[col_grp.col, col_cat.col]]
            .sort_values([col_grp.col, col_cat.col])
            .reset_index(drop=True)
        )
        if q_grp:
            ref_df = ref_df[ref_df[col_grp.col].str.contains(q_grp, case=False, na=False)]
        if q_cat:
            ref_df = ref_df[ref_df[col_cat.col].str.contains(q_cat, case=False, na=False)]
        st.dataframe(
            ref_df,
            width="stretch",
            hide_index=True,
            height=600,
            column_config={k: st.column_config.TextColumn(label=v) for k, v in COL_LABELS.items()},
        )

# ── Speichern ─────────────────────────────────────────────────────────────────
# rows_changed kommt aus dem von Streamlit selbst geführten edited_rows-Dict
# (siehe write_changes) statt aus einem vollständigen DataFrame-Vergleich.
n_edits = len(st.session_state.get("main_editor", {}).get("edited_rows", {}))
st.write(n_edits, "Zeile(n) geändert")

st.button(
    label="💾 Änderungen in Datenbank speichern",
    icon="🔥",
    disabled=(n_edits < 1),
    on_click=write_changes,
    args=(df_display, "main_editor", var_acc),
)