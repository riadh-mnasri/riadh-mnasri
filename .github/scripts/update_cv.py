"""Ajoute au CV GitHub (README.md / README.en.md) les nouveaux repos publics.

Un repo est candidat s'il est public, ni fork ni archivé, créé après la date
`since` de .github/cv-config.json, absent des deux README et absent de la liste
`ignore`. Claude lit son README et rédige une ligne FR et une ligne EN pour le
tableau "Autres projets" / "Other projects".

Usage : python update_cv.py [--dry-run] [--since AAAA-MM-JJ] [--limit N]
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / ".github" / "cv-config.json").read_text(encoding="utf-8"))
OWNER = CONFIG["owner"]
README_FR = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
TABLE_HEADER_FR = "| Aperçu | Projet | Description | Stack |"
TABLE_HEADER_EN = "| Preview | Project | Description | Stack |"

SCHEMA = {
    "type": "object",
    "properties": {
        "include": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "emoji": {"type": "string"},
        "title": {"type": "string"},
        "description_fr": {"type": "string"},
        "description_en": {"type": "string"},
        "stack": {"type": "string"},
    },
    "required": ["include", "skip_reason", "emoji", "title", "description_fr", "description_en", "stack"],
    "additionalProperties": False,
}

SYSTEM = """Tu mets à jour le CV GitHub de Riadh MNASRI, Senior Tech Lead & Architecte Java/Kotlin, freelance.
On te donne un repo public récent. Décide s'il mérite une ligne dans le tableau "Autres projets", puis rédige-la.

Mets include=false (et explique en une phrase dans skip_reason) si le repo est vide, un simple squelette,
un test jetable, un fork sans apport, ou s'il n'a rien de montrable à un client ou un recruteur.
Sinon include=true et skip_reason="".

Règles d'écriture, strictes :
- title : nom court du projet tel qu'il se présente dans son README (pas forcément le slug du repo).
- emoji : un seul emoji lié au domaine du projet.
- description_fr / description_en : une phrase factuelle de 15 à 30 mots, ce que fait le projet et ce qu'il a de notable. Pas de superlatifs, pas de marketing.
- Français avec tous les accents. Anglais naturel. Les deux disent la même chose.
- Jamais de tiret cadratin ni de tiret demi-cadratin. Utilise ":", ",", ";" ou des parenthèses.
- Jamais de caractère "|" (on est dans un tableau Markdown). Pas de lien Markdown dans les descriptions.
- stack : 2 à 4 technologies principales séparées par " · " (ex. "Kotlin · Spring Boot · Kafka").
- N'invente rien qui ne soit pas dans les données fournies."""


def github(path):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def public_repos():
    repos, page = [], 1
    while True:
        batch = github(f"/users/{OWNER}/repos?type=owner&per_page=100&page={page}")
        repos += batch
        if len(batch) < 100:
            return repos
        page += 1


def readme_of(name):
    try:
        data = github(f"/repos/{OWNER}/{name}/readme")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def mentioned(repo, text):
    lowered = text.lower()
    if f"github.com/{OWNER}/{repo['name']}".lower() in lowered:
        return True
    homepage = (repo.get("homepage") or "").strip().rstrip("/")
    if homepage:
        host = re.sub(r"^https?://(www\.)?", "", homepage).split("/")[0].lower()
        if host and host in lowered:
            return True
    return False


def clean(text, lang):
    text = text.replace("|", "/").replace("\n", " ").strip()
    sep = " : " if lang == "fr" else ": "
    text = re.sub(r"\s*[—–]\s*", sep, text)
    return re.sub(r"\s{2,}", " ", text)


def draft(client, repo, readme):
    payload = {
        "name": repo["name"],
        "description": repo.get("description") or "",
        "homepage": repo.get("homepage") or "",
        "language": repo.get("language") or "",
        "topics": repo.get("topics") or [],
        "created_at": repo["created_at"],
        "readme": readme,
    }
    response = client.beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    if response.stop_reason != "end_turn":
        print(f"  {repo['name']} : réponse inutilisable (stop_reason={response.stop_reason}), ignoré ce coup-ci")
        return None
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def row(repo, d, lang):
    homepage = (repo.get("homepage") or "").strip()
    repo_url = repo["html_url"]
    link = homepage or repo_url
    desc = clean(d[f"description_{lang}"], lang).rstrip(".")
    if homepage:
        label = "démo" if lang == "fr" else "demo"
        desc += f" ([{label}]({homepage}))"
    title = clean(d["title"], lang)
    return f"| | {d['emoji'].strip()} [{title}]({link}) | {desc} | {clean(d['stack'], lang)} |"


def insert_rows(path, header, rows):
    lines = path.read_text(encoding="utf-8").split("\n")
    start = lines.index(header)
    end = start
    while end + 1 < len(lines) and lines[end + 1].startswith("|"):
        end += 1
    lines[end + 1:end + 1] = rows
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", default=CONFIG["since"])
    parser.add_argument("--limit", type=int, default=CONFIG.get("max_per_run", 3))
    args = parser.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    ignore = {name.lower() for name in CONFIG.get("ignore", [])}
    readmes = README_FR.read_text(encoding="utf-8") + README_EN.read_text(encoding="utf-8")

    candidates = [
        r for r in public_repos()
        if not r["fork"] and not r["archived"] and not r["private"] and r["size"] > 0
        and r["name"].lower() != OWNER.lower()
        and r["name"].lower() not in ignore
        and datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= since
        and not mentioned(r, readmes)
    ]
    candidates.sort(key=lambda r: r["created_at"])
    print(f"{len(candidates)} nouveau(x) repo(s) public(s) depuis {args.since} : {[r['name'] for r in candidates]}")
    if not candidates:
        return

    client = anthropic.Anthropic()
    rows_fr, rows_en, added = [], [], []
    for repo in candidates[:args.limit]:
        readme = readme_of(repo["name"])
        if len(readme.strip()) < 200 and not repo.get("description"):
            print(f"  {repo['name']} : pas encore de README ni de description, on attend")
            continue
        d = draft(client, repo, readme)
        if d is None:
            continue
        if not d["include"]:
            print(f"  {repo['name']} : écarté par Claude ({d['skip_reason']})")
            continue
        rows_fr.append(row(repo, d, "fr"))
        rows_en.append(row(repo, d, "en"))
        added.append(repo["name"])

    for line in rows_fr + rows_en:
        print("  + " + line)
    if not added:
        return
    if args.dry_run:
        print("Dry run : aucun fichier modifié.")
        return
    insert_rows(README_FR, TABLE_HEADER_FR, rows_fr)
    insert_rows(README_EN, TABLE_HEADER_EN, rows_en)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"added={', '.join(added)}\n")


if __name__ == "__main__":
    sys.exit(main())
