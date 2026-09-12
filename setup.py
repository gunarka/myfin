#!/usr/bin/env python3
"""
setup.py
Ersteinrichtung nach dem Klonen: Legt .data/-Verzeichnis und leere
Datenbanktabellen an. Der Keyring wird beim ersten App-Start erzeugt,
sobald ein Master-Passwort eingegeben wird.

Ausführen:  python setup.py
"""
from pathlib import Path
import sys

DATA_DIR = Path(__file__).parent / ".data"
DB_PATH  = DATA_DIR / "bookings.duckdb"


def main() -> int:
    try:
        import duckdb
    except ImportError:
        print("FEHLER: duckdb nicht gefunden.")
        print("Bitte zuerst installieren:  pip install -r requirements.txt")
        return 1

    sys.path.insert(0, str(Path(__file__).parent))
    from db_schema import ensure_core_tables

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Verzeichnis  {DATA_DIR}")

    con = duckdb.connect(str(DB_PATH))
    ensure_core_tables(con)
    con.close()
    print(f"[OK] Datenbank    {DB_PATH}")

    print("""
Nächste Schritte:
  1. Virtuelle Umgebung erstellen:
       python -m venv .venv
  2. Pakete installieren:
       .venv/bin/pip install -r requirements.txt
  3. App starten:
       streamlit run app.py
  4. Beim ersten Start wird ein Master-Passwort abgefragt –
     dieses Passwort verschlüsselt den Keyring (.data/keyring.cfg).
     Bitte sicher aufbewahren; ohne es sind die Zugangsdaten nicht
     wiederherstellbar.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
