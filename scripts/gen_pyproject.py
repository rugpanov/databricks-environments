#!/usr/bin/env python3
"""Generate a single environment's pyproject.toml (and constraints.txt) from a
local Databricks requirements file. Thin CLI around ``envgen``.

Usage:
    gen_pyproject.py <requirements-env-N.txt> <env_name> <python_version> [out_dir]
        env_name        e.g. serverless-v4
        python_version  full version, e.g. 3.12.3
        out_dir         optional; if given, writes pyproject.toml + constraints.txt
                        there. Otherwise prints the pyproject.toml to stdout.

Examples:
    gen_pyproject.py requirements-env-4.txt serverless-v4 3.12.3
    gen_pyproject.py requirements-env-4.txt serverless-v4 3.12.3 python/serverless/serverless-v4
"""
import os
import sys

import envgen


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    req, env_name, pyver = sys.argv[1], sys.argv[2], sys.argv[3]
    out_dir = sys.argv[4] if len(sys.argv) == 5 else None

    pkgs = envgen.parse_requirements(open(req, encoding="utf-8").read())
    pyproject = envgen.build_pyproject(pkgs, env_name, pyver)

    if not out_dir:
        sys.stdout.write(pyproject)
        return

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(pyproject)
    with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
        f.write(envgen.build_constraints(pkgs, env_name))
    print(f"wrote {out_dir}/pyproject.toml and constraints.txt")


if __name__ == "__main__":
    main()
