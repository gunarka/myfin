"""
app.py
Einstiegspunkt der MyFin-Anwendung.
Konfiguriert Streamlit und registriert alle Unterseiten.
"""

import streamlit as st
import app_functions as f

# ── Seitenkonfiguration ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="MyFin",
    page_icon="🔖",
    layout="wide",
)

# ── Seitenregistrierung & Navigation ─────────────────────────────────────────
pages = {
    "pages": [
        st.Page("app_dashboard.py"),
        st.Page("app_assign.py"),
        st.Page("app_forecast.py"),
        st.Page("app_retrieve.py"),
        st.Page("app_admin.py"),
    ]
}

pg = st.navigation(pages, position="hidden")

# Sidebar VOR pg.run() rendern – sonst wird sie nicht angezeigt, wenn die
# aufgerufene Seite mit st.stop() oder require_master_password() abbricht.
f.navigation()

pg.run()
