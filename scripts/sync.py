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
import urllib.error
import urllib.request

import envgen

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERLESS_PAGE = "https://docs.databricks.com/aws/en/release-notes/serverless/environment-version/{word}"
DBR_INDEX = "https://docs.databricks.com/aws/en/release-notes/runtime/"
DBR_PAGE = "https://docs.databricks.com/aws/en/release-notes/runtime/{slug}"
DOCS_HOST = "https://docs.databricks.com"
WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve"]

# Index entries that aren't a runtime version page.
DBR_NON_VERSION = {"maintenance-updates", "databricks-runtime-ver", "eos"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "databricks-environments-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def discover_serverless():
    """Return [(n, page_html)] for every serverless version page that exists.

    Only a genuine HTTP 404 counts as "this version doesn't exist" (end-of-list).
    Transient errors (timeout, 5xx) must not be mistaken for the end of the list —
    otherwise a flaky run silently drops every higher version — so they're logged
    and skipped without advancing the end-of-list counter.
    """
    found = []
    misses = 0
    for n, word in enumerate(WORDS, start=1):
        try:
            html = fetch(SERVERLESS_PAGE.format(word=word))
            found.append((n, html))
            misses = 0
        except urllib.error.HTTPError as e:
            if e.code == 404:
                misses += 1
                # Only treat trailing 404s as end-of-list. Retired early versions
                # (e.g. v1/v2 removed) must not stop us before reaching live ones,
                # so require at least one found version before honoring the break.
                if found and misses >= 2:
                    break
            else:
                print(f"  ! serverless v{n}: transient HTTP {e.code}; skipping (not end-of-list)")
        except Exception as e:
            print(f"  ! serverless v{n}: transient fetch error ({e}); skipping (not end-of-list)")
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
        try:
            pkgs = envgen.parse_requirements(fetch(req_url))
        except Exception as e:
            # Don't let one flaky download abort the rest of the sync (DBR runs after).
            print(f"  ! {env_name}: download failed ({e}); skipping")
            continue
        out_dir = os.path.join(REPO, "python", "serverless", env_name)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write(envgen.build_pyproject(pkgs, env_name, python_version))
        with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
            f.write(envgen.build_constraints(pkgs, env_name))
        print(f"  + {env_name} (python {python_version}, {len(pkgs)} packages)")
        written.append(env_name)
    return written


def table_pkgs(html, anchor_id):
    """Parse the first ``<table>`` after the heading with ``id=<anchor_id>`` into
    {normalized_name: version}.

    Cells alternate Library, Version across however many column pairs the page uses
    (``<td><p>name<td><p>version ...``). We anchor on the heading's anchor id rather
    than its text — some pages mention the phrase earlier in a changelog and carry
    other tables (e.g. dated maintenance tables) we must not capture.
    """
    m = re.search(r'id=["\']?' + re.escape(anchor_id), html)
    if not m:
        return None
    t0 = html.find("<table>", m.end())
    t1 = html.find("</table>", t0)
    if t0 == -1 or t1 == -1:
        return None
    # Split on <td> and strip any inline tags (<p>, nested <a>/<code>, etc.) rather
    # than assuming every cell is exactly "<td><p>text" — a cell that deviates from
    # that shape would otherwise be dropped and shift name/version alignment for the
    # rest of the table. Header <th> cells are naturally excluded.
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in html[t0:t1].split("<td>")[1:]]
    return {envgen.norm(cells[k]): cells[k + 1]
            for k in range(0, len(cells) - 1, 2) if cells[k]}


def dbr_meta(html):
    """Return (version, scala, python_version) from a standard runtime page, e.g.
    ('17.3', '2.13', '3.12.3'), or None if any piece is missing."""
    # Anchor the version to the page's own <title> rather than the first generic
    # "Databricks Runtime N" in the HTML — sidebar/nav links to other runtimes can
    # appear earlier in the source and would otherwise be mis-selected.
    ver = re.search(r"<title[^>]*>Databricks Runtime\s+(\d+)(?:\.(\d+))?", html)
    sc = re.search(r"Scala</strong>\s*:\s*(\d+\.\d+)", html)
    pv = re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
    if not (ver and sc and pv):
        return None
    return f"{ver.group(1)}.{ver.group(2) or '0'}", sc.group(1), pv.group(1)


def parse_dbr_page(html):
    """Extract (pkgs, python_version) from a standard DBR runtime page."""
    pkgs = table_pkgs(html, "installed-python-libraries")
    pv = re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
    return pkgs, (pv.group(1) if pv else None)


def discover_dbr(ml=False):
    """Enumerate runtime version slugs from the release-notes index.

    ml=False -> standard runtimes (e.g. '17.3lts', '19');
    ml=True  -> ML variants     (e.g. '17.3lts-ml', '19ml').
    """
    html = fetch(DBR_INDEX)
    slugs = re.findall(r"release-notes/runtime/([0-9][\w.-]*)", html)
    pat = r"^\d+(\.\d+)?(lts)?-?ml$" if ml else r"^\d+(\.\d+)?(lts)?$"
    out, seen = [], set()
    for s in slugs:
        s = s.rstrip("/")
        if s in seen or s in DBR_NON_VERSION or s.endswith("ml") != ml:
            continue
        if re.match(pat, s):
            seen.add(s)
            out.append(s)
    return out


def _write_env(key, pkgs, python_version, dbconnect):
    out_dir = os.path.join(REPO, "python", "dbr", key)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(envgen.build_pyproject(pkgs, key, python_version, dbconnect=dbconnect))
    with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
        f.write(envgen.build_constraints(pkgs, key))
    print(f"  + dbr/{key} (python {python_version}, {len(pkgs)} packages)")


def sync_dbr():
    for slug in discover_dbr():
        try:
            html = fetch(DBR_PAGE.format(slug=slug))
        except Exception as e:
            print(f"  ! dbr [{slug}]: fetch failed ({e}); skipping")
            continue
        meta = dbr_meta(html)
        pkgs, _ = parse_dbr_page(html)
        if not meta or not pkgs:
            print(f"  ! dbr [{slug}]: no meta / Python table; skipping")
            continue
        ver, scala, python_version = meta
        _write_env(f"{ver}.x-scala{scala}", pkgs, python_version, ver)


def ml_variant_pkgs(ml_html, variant):
    """Python packages for an ML cluster variant ('cpu' or 'gpu').

    Newer ML pages link a downloadable ``requirements-{cpu,gpu}-<slug>.txt``; older
    ones render the list inline under ``python-libraries-on-{cpu,gpu}-clusters``.
    """
    m = re.search(r"(/[\w/-]*assets/files/requirements-" + variant + r"-[\w.-]+\.txt)", ml_html)
    if m:
        return envgen.parse_requirements(fetch(DOCS_HOST + m.group(1)))
    return table_pkgs(ml_html, f"python-libraries-on-{variant}-clusters")


def sync_dbr_ml():
    for slug in discover_dbr(ml=True):
        base = re.sub(r"-?ml$", "", slug)        # 17.3lts-ml -> 17.3lts ; 19ml -> 19
        try:
            base_html = fetch(DBR_PAGE.format(slug=base))
            ml_html = fetch(DBR_PAGE.format(slug=slug))
        except Exception as e:
            print(f"  ! dbr-ml [{slug}]: fetch failed ({e}); skipping")
            continue
        meta = dbr_meta(base_html)               # ML pages lack System environment; use base
        if not meta:
            print(f"  ! dbr-ml [{slug}]: no base meta from {base}; skipping")
            continue
        ver, scala, python_version = meta
        for variant in ("cpu", "gpu"):
            pkgs = ml_variant_pkgs(ml_html, variant)
            if not pkgs:
                print(f"  ! dbr-ml [{slug}] {variant}: no packages found; skipping")
                continue
            _write_env(f"{ver}.x-{variant}-ml-scala{scala}", pkgs, python_version, ver)


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

    if args.check:
        # --check restores python/ afterwards (checkout + clean), which would destroy
        # any pre-existing uncommitted work there. Refuse rather than clobber it.
        pre = git("status", "--porcelain", "python/").strip()
        if pre:
            print("Refusing --check: python/ has uncommitted changes that would be "
                  "discarded by the post-check restore. Commit or stash them first.")
            sys.exit(2)

    print("Discovering serverless environments from docs.databricks.com ...")
    sync_serverless()
    print("Syncing DBR runtimes from docs.databricks.com ...")
    sync_dbr()
    print("Syncing DBR ML runtimes (CPU + GPU) from docs.databricks.com ...")
    sync_dbr_ml()
    changed = reconcile()

    if args.check:
        # Safe now: python/ was verified clean above, so this only reverts/removes
        # what this run generated.
        git("checkout", "--", "python/")
        git("clean", "-fdq", "python/")
        sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
