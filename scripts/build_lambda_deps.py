"""Vendor the ingest Lambda's third-party dependencies.

The ingest Lambda needs `pypdf` to read the one PDF in the sample knowledge base. It is
pure Python, so `pip install -t` produces a portable artifact from any operating system --
no Docker, no manylinux wheels, no CDK bundling image. That is the whole reason the
dependency list is one entry long.

    python scripts/build_lambda_deps.py

Run before `cdk synth` or `cdk deploy`. The output directory is gitignored: it is a build
artifact, not source.

If a dependency with compiled extensions is ever added, this stops being enough -- it would
need `--platform manylinux2014_x86_64 --only-binary=:all:` to fetch Linux wheels from a
Windows or macOS machine, and the query Lambda's promise of zero dependencies would be
worth re-examining first.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "services" / "ingest" / "vendor"
DEPENDENCIES = ["pypdf>=5,<7"]


def main() -> int:
    if VENDOR_DIR.exists():
        shutil.rmtree(VENDOR_DIR)
    VENDOR_DIR.mkdir(parents=True)

    print(f"Installing {', '.join(DEPENDENCIES)} into {VENDOR_DIR.relative_to(ROOT)}/")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(VENDOR_DIR), *DEPENDENCIES],
        check=False,
    )
    if result.returncode != 0:
        print("pip failed; the ingest Lambda cannot be packaged.", file=sys.stderr)
        return result.returncode

    # Metadata directories add weight to every deployment for no runtime benefit.
    for junk in list(VENDOR_DIR.glob("*.dist-info")) + list(VENDOR_DIR.glob("__pycache__")):
        shutil.rmtree(junk, ignore_errors=True)

    size = sum(f.stat().st_size for f in VENDOR_DIR.rglob("*") if f.is_file())
    packages = sorted(p.name for p in VENDOR_DIR.iterdir() if p.is_dir())
    print(f"Vendored {', '.join(packages)} -- {size / 1024:.0f} KiB")

    try:
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); import pypdf" % VENDOR_DIR],
            check=True,
        )
        print("Import check passed.")
    except subprocess.CalledProcessError:
        print("Vendored package does not import; the Lambda would fail at runtime.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
