#!/usr/bin/env python3
"""Weekly sync: discover newly published Databricks environments from the public
release notes, regenerate the pinned artifacts, and reconcile them against what is
committed in this repo.

Serverless is the reliable path: each environment-version release-notes page links a
downloadable ``requirements-env-N.txt`` (a clean ``name==version`` list). This script
discovers the available versions, downloads each list, applies the transformation
rules (see ``envgen``), and writes ``python/serverless/serverless-vN/{pyproject.toml,
constraints.txt}``.

DBR is a TODO: those pages list libraries inline in HTML (no downloadable file), so
parsing is more brittle and is intentionally left as a follow-up.

Modes:
    python scripts/sync.py            # regenerate into the working tree
    python scripts/sync.py --check    # regenerate, then exit 1 if anything changed
                                      # (drift / new versions) without leaving edits

Reconciliation is delegated to git: after regeneration, ``git status --porcelain``
on ``python/`` shows changed (drift) and untracked (new version) artifacts. In
``--check`` mode the script restores the working tree and returns non-zero so CI can
open a PR.
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.request

import envgen

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERLESS_PAGE = "https://docs.databricks.com/aws/en/release-notes/serverless/environment-version/{word}"
DOCS_HOST = "https://docs.databricks.com"
WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "databricks-environments-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def discover_serverless():
    """Return [(n, page_html)] for every serverless version page that exists."""
    found = []
    misses = 0
    for n, word in enumerate(WORDS, start=1):
        try:
            html = fetch(SERVERLESS_PAGE.format(word=word))
            found.append((n, html))
            misses = 0
        except Exception:
            misses += 1
            if misses >= 2:        # two consecutive 404s -> stop probing
                break
    return found


def parse_page(html):
    """Extract (requirements_url, python_version) from a version page.

    The Python version lives in the "System environment" list as
    ``Python</strong>: 3.12.3``. We match that precise form rather than the first
    ``3.x.y`` on the page — the package table is full of ``3.x.y`` versions, and a
    loose match silently picks a wrong one. If neither precise form is found we
    return None so the caller skips (never emits a guessed version).
    """
    m = re.search(r"(/[\w/-]*assets/files/requirements-env-\d+-[0-9a-f]+\.txt)", html)
    req_url = DOCS_HOST + m.group(1) if m else None
    pv = (re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
          or re.search(r"Python version[^0-9]{0,40}?(\d+\.\d+\.\d+)", html))
    python_version = pv.group(1) if pv else None
    return req_url, python_version


def sync_serverless():
    written = []
    for n, html in discover_serverless():
        env_name = f"serverless-v{n}"
        req_url, python_version = parse_page(html)
        if not req_url or not python_version:
            print(f"  ! {env_name}: could not locate requirements URL / python version; skipping")
            continue
        pkgs = envgen.parse_requirements(fetch(req_url))
        out_dir = os.path.join(REPO, "python", "serverless", env_name)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write(envgen.build_pyproject(pkgs, env_name, python_version))
        with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
            f.write(envgen.build_constraints(pkgs, env_name))
        print(f"  + {env_name} (python {python_version}, {len(pkgs)} packages)")
        written.append(env_name)
    return written


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args],
                          capture_output=True, text=True).stdout


def reconcile():
    """Print drift (modified) and new (untracked) artifacts under python/."""
    status = git("status", "--porcelain", "python/").strip()
    if not status:
        print("\nReconciliation: no changes — repo is in sync with published docs.")
        return False
    changed, new = [], []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        (new if "?" in code else changed).append(path)
    print("\nReconciliation: changes detected")
    for p in new:
        print(f"  NEW    {p}")
    for p in changed:
        print(f"  DRIFT  {p}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="regenerate, report drift/new, restore tree, exit 1 if changed")
    args = ap.parse_args()

    print("Discovering serverless environments from docs.databricks.com ...")
    sync_serverless()
    changed = reconcile()

    if args.check:
        git("checkout", "--", "python/")
        git("clean", "-fdq", "python/")
        sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
