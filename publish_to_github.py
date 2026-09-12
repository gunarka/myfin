#!/usr/bin/env python3
"""
Veröffentlicht dieses Projekt auf GitHub – ohne sensible Daten.

Voraussetzungen:
  - Git ist installiert
  - Personal Access Token (Scope "repo") als Umgebungsvariable:
        export GITHUB_TOKEN=ghp_xxx        (Linux/Mac)
        setx GITHUB_TOKEN ghp_xxx          (Windows)
  - `pip install requests` (falls nicht schon vorhanden)

Nutzung:
  python publish_to_github.py --name myfin --private
"""

import argparse
import fnmatch
import os
import subprocess
import sys

import requests

API_URL = "https://api.github.com"

# Muster, die NIEMALS ins Repo gelangen dürfen (Ergänzung zu .gitignore als
# zweite Sicherheitsschicht direkt vor dem Commit)
SENSITIVE_PATTERNS = [
    ".data/*", "*.duckdb", "*.duckdb.wal", "*.db", "*.sqlite", "*.sqlite3",
    "keyring.cfg", "backups/*", ".env", ".env.*", "secrets.toml",
    ".streamlit/secrets.toml", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
]


def run(cmd: list[str], cwd: str) -> str:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        sys.exit(f"Fehler bei '{' '.join(cmd)}':\n{result.stderr}")
    return result.stdout.strip()


def ensure_gitignore(repo_dir: str) -> None:
    """Legt ein .gitignore an, falls noch keines existiert."""
    path = os.path.join(repo_dir, ".gitignore")
    if not os.path.exists(path):
        sys.exit(".gitignore fehlt – bitte zuerst anlegen (siehe mitgeliefertes Template).")


def check_no_sensitive_files_staged(repo_dir: str) -> None:
    """Bricht ab, falls trotz .gitignore sensible Dateien zum Commit vorgemerkt sind."""
    staged = run(["git", "diff", "--cached", "--name-only"], repo_dir).splitlines()
    offenders = [
        f for f in staged
        if any(fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(os.path.basename(f), pat)
               for pat in SENSITIVE_PATTERNS)
    ]
    if offenders:
        sys.exit(
            "Abbruch: folgende sensible Dateien sind gestaged und würden hochgeladen:\n"
            + "\n".join(f"  - {f}" for f in offenders)
            + "\nBitte .gitignore prüfen und `git rm --cached <datei>` ausführen."
        )


def create_github_repo(token: str, name: str, description: str, private: bool) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = requests.post(
        f"{API_URL}/user/repos",
        headers=headers,
        json={"name": name, "description": description, "private": private},
        timeout=30,
    )
    if resp.status_code == 422:
        sys.exit(f"Repo '{name}' existiert bereits oder Name ungültig:\n{resp.text}")
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="MyFin sicher auf GitHub veröffentlichen")
    parser.add_argument("--name", default="myfin", help="Repository-Name")
    parser.add_argument("--description", default="Lokale Streamlit-App zur Finanzverwaltung")
    parser.add_argument("--private", action="store_true", help="Repo als privat anlegen")
    parser.add_argument("--path", default=".", help="Pfad zum Projektverzeichnis")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN ist nicht gesetzt. Siehe Kommentar am Skriptanfang.")

    repo_dir = os.path.abspath(args.path)
    ensure_gitignore(repo_dir)

    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        run(["git", "init", "-b", args.branch], repo_dir)

    run(["git", "add", "-A"], repo_dir)
    check_no_sensitive_files_staged(repo_dir)

    diff = run(["git", "diff", "--cached", "--name-only"], repo_dir)
    if diff:
        run(["git", "commit", "-m", "Initial commit"], repo_dir)
    else:
        print("Nichts zu committen.")

    print(f"Erstelle GitHub-Repo '{args.name}' ...")
    repo = create_github_repo(token, args.name, args.description, args.private)
    owner = repo["owner"]["login"]

    # Remote ohne Token dauerhaft speichern
    remotes = run(["git", "remote"], repo_dir).split()
    remote_url = f"https://github.com/{owner}/{args.name}.git"
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", remote_url], repo_dir)
    else:
        run(["git", "remote", "add", "origin", remote_url], repo_dir)

    # Push: Token nur temporär in der Push-URL, wird nicht gespeichert
    push_url = f"https://{token}@github.com/{owner}/{args.name}.git"
    run(["git", "push", "-u", push_url, args.branch], repo_dir)

    print(f"Fertig: {repo['html_url']}")


if __name__ == "__main__":
    main()
