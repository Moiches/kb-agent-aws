"""Vendor the Lambda functions' third-party dependencies.

    python scripts/build_lambda_deps.py

Run before `cdk synth` or `cdk deploy` (`npm run deploy` chains it). Both output directories
are gitignored: they are build artifacts, not source.

Two targets, one command:

INGEST -- services/ingest/vendor/. The ingest Lambda needs `pypdf` to read the one PDF in the
sample knowledge base. It is pure Python, so a plain `pip install -t` produces a portable
artifact from any operating system -- no Docker, no manylinux wheels, no CDK bundling image.
That is the whole reason the dependency list is one entry long.

QUERY -- services/query/vendor/ (this branch only; on main the query Lambda has no third-party
dependencies, ADR-02). The verification experiment orchestrates the query with LangGraph, whose
closure carries eleven native extension modules (pydantic-core, zstandard, orjson, ...). A plain
`pip install -t` on Windows would vendor win_amd64 `.pyd` files that die with ImportError at
the first cold start, so this target asks pip for Linux cp312 wheels explicitly and refuses
anything it would have to build from source. The pins in services/query/requirements-lambda.txt
are exact because the closure is what was audited, not a version range.

What this script cannot do is import the query closure: Linux extension modules do not load on
Windows, and the development machine has neither Docker nor WSL. The checks after the install
catch every mistake that is visible from the filesystem -- a Windows wheel, a wrong ABI tag, a
distribution nobody pinned, an oversize tree -- and fail the build rather than the deploy. The
first `GET /health` after deploy is the import test: it exercises every eager import, and an
ImportError there surfaces as a 500 on every route.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INGEST_VENDOR_DIR = ROOT / "services" / "ingest" / "vendor"
INGEST_DEPENDENCIES = ["pypdf>=5,<7"]

QUERY_VENDOR_DIR = ROOT / "services" / "query" / "vendor"
QUERY_REQUIREMENTS = ROOT / "services" / "query" / "requirements-lambda.txt"
# The query Lambda is python3.12 on Amazon Linux 2023, x86_64, glibc 2.34. Every flag is
# load-bearing: --platform selects Linux wheels instead of win_amd64; --only-binary makes pip
# refuse to build from source (a build here would silently be a Windows build, and a 3.11
# interpreter could not build for 3.12 anyway); the last three select cp312 wheels even though
# the interpreter running pip is 3.11.
QUERY_PIP_PLATFORM = [
    "--platform", "manylinux2014_x86_64",
    "--only-binary=:all:",
    "--python-version", "3.12",
    "--implementation", "cp",
    "--abi", "cp312",
]
# Lambda allows "250 MB" unzipped for function code plus layers; the quota is 262,144,000
# bytes, so it is MiB and so is this. The closure measured 47.7 MiB pruned; 200 leaves room
# for the handler and for a pin bump that pulls in one more native module, while still
# catching a resolution that went somewhere unexpected (numpy, say).
QUERY_SIZE_LIMIT_MIB = 200

# What a correctly targeted wheel leaves behind. A wheel is either pure Python (py2/py3 tags;
# pip writes a `Tag:` line per tag, so a py2.py3 wheel shows as two lines) or a cp312 x86_64
# manylinux wheel of any glibc vintage -- the closure spans manylinux1, 2014, _2_5, _2_17 and
# _2_28, and AL2023's glibc 2.34 satisfies all of them.
PURE_WHEEL_TAGS = {"py3-none-any", "py2-none-any", "py2.py3-none-any"}
MANYLINUX_CP312_TAG = re.compile(r"cp312-cp312-manylinux(?:\d+|_\d+_\d+)_x86_64")
LINUX_EXTENSION_MARKER = "cpython-312-x86_64-linux-gnu"


def build_ingest() -> int:
    if INGEST_VENDOR_DIR.exists():
        shutil.rmtree(INGEST_VENDOR_DIR)
    INGEST_VENDOR_DIR.mkdir(parents=True)

    print(f"Installing {', '.join(INGEST_DEPENDENCIES)} into {INGEST_VENDOR_DIR.relative_to(ROOT)}/")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(INGEST_VENDOR_DIR), *INGEST_DEPENDENCIES],
        check=False,
    )
    if result.returncode != 0:
        print("pip failed; the ingest Lambda cannot be packaged.", file=sys.stderr)
        return result.returncode

    # Metadata directories add weight to every deployment for no runtime benefit.
    for junk in list(INGEST_VENDOR_DIR.glob("*.dist-info")) + list(INGEST_VENDOR_DIR.glob("__pycache__")):
        shutil.rmtree(junk, ignore_errors=True)

    size = sum(f.stat().st_size for f in INGEST_VENDOR_DIR.rglob("*") if f.is_file())
    packages = sorted(p.name for p in INGEST_VENDOR_DIR.iterdir() if p.is_dir())
    print(f"Vendored {', '.join(packages)} -- {size / 1024:.0f} KiB")

    try:
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); import pypdf" % INGEST_VENDOR_DIR],
            check=True,
        )
        print("Import check passed.")
    except subprocess.CalledProcessError:
        print("Vendored package does not import; the Lambda would fail at runtime.", file=sys.stderr)
        return 1

    return 0


def _pinned_names(requirements: Path) -> set[str]:
    """Distribution names pinned in a requirements file, normalised the way dist-info names are."""
    names = set()
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(_normalise(line.split("==", 1)[0]))
    return names


def _normalise(name: str) -> str:
    return name.lower().replace("-", "_")


def _verify_query_vendor() -> list[str]:
    """Everything wrong with the tree that can be seen without a Linux interpreter."""
    problems = []

    # A .pyd is a Windows extension module: it means pip fell back to a win_amd64 wheel, which
    # is exactly the artifact the platform flags exist to prevent.
    for pyd in sorted(QUERY_VENDOR_DIR.rglob("*.pyd")):
        problems.append(f"Windows extension module: {pyd.relative_to(QUERY_VENDOR_DIR)}")

    # Every extension module must be built for the runtime's interpreter and architecture; a
    # cp311 or aarch64 .so fails the same way a .pyd does, just less obviously.
    for so in sorted(QUERY_VENDOR_DIR.rglob("*.so")):
        if LINUX_EXTENSION_MARKER not in so.name:
            problems.append(f"extension module not tagged {LINUX_EXTENSION_MARKER}: {so.relative_to(QUERY_VENDOR_DIR)}")

    # The wheel tags are the same check at the distribution level, and the only one that sees
    # a wheel with no extension module in it at all.
    for wheel in sorted(QUERY_VENDOR_DIR.glob("*.dist-info/WHEEL")):
        for line in wheel.read_text(encoding="utf-8").splitlines():
            if not line.startswith("Tag:"):
                continue
            tag = line.split(":", 1)[1].strip()
            if tag not in PURE_WHEEL_TAGS and not MANYLINUX_CP312_TAG.fullmatch(tag):
                problems.append(f"{wheel.parent.name}: wheel tag {tag} is neither pure Python nor cp312 x86_64 manylinux")

    # pip resolves transitive dependencies even when the requirements file lists them all, so
    # a pin that goes missing would be installed silently at whatever version resolved today.
    # Requiring every installed distribution to be pinned keeps the audited closure the
    # deployed closure.
    installed = {_normalise(p.name.split("-", 1)[0]) for p in QUERY_VENDOR_DIR.glob("*.dist-info")}
    for unpinned in sorted(installed - _pinned_names(QUERY_REQUIREMENTS)):
        problems.append(f"{unpinned} was installed but is not pinned in {QUERY_REQUIREMENTS.relative_to(ROOT)}")

    return problems


def build_query() -> int:
    # pip refuses to install into a --target that already holds the packages.
    if QUERY_VENDOR_DIR.exists():
        shutil.rmtree(QUERY_VENDOR_DIR)
    QUERY_VENDOR_DIR.mkdir(parents=True)

    print(f"Installing {QUERY_REQUIREMENTS.relative_to(ROOT)} into {QUERY_VENDOR_DIR.relative_to(ROOT)}/ (Linux cp312 wheels)")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(QUERY_VENDOR_DIR),
         *QUERY_PIP_PLATFORM, "-r", str(QUERY_REQUIREMENTS)],
        check=False,
    )
    if result.returncode != 0:
        print("pip failed; the query Lambda cannot be packaged.", file=sys.stderr)
        return result.returncode

    problems = _verify_query_vendor()
    if problems:
        print("The vendored tree would not run on Lambda:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # Prune what the runtime cannot use. The bytecode was compiled by the 3.11 interpreter
    # that ran pip, so the 3.12 runtime would ignore it (17 MiB). bin/ holds console-script
    # launchers pip writes for the *host* -- .exe files, in a Linux artifact (0.7 MiB). The
    # *.dist-info directories stay, unlike the ingest target. `import langgraph.graph` was
    # checked to survive without them, which is exactly the problem: langchain_core, langsmith
    # and pydantic read versions through importlib.metadata lazily, so a missing dist-info
    # would pass the GET /health gate and raise PackageNotFoundError mid-request instead.
    # 1 MiB is not worth an error the deploy gate cannot see.
    for junk in list(QUERY_VENDOR_DIR.rglob("__pycache__")) + [QUERY_VENDOR_DIR / "bin"]:
        shutil.rmtree(junk, ignore_errors=True)

    size = sum(f.stat().st_size for f in QUERY_VENDOR_DIR.rglob("*") if f.is_file())
    size_mib = size / 1024**2
    distributions = len(list(QUERY_VENDOR_DIR.glob("*.dist-info")))
    native = sorted(so.relative_to(QUERY_VENDOR_DIR).as_posix() for so in QUERY_VENDOR_DIR.rglob("*.so"))
    print(f"Vendored {distributions} distributions -- {size_mib:.1f} MiB unzipped, {len(native)} native extension modules:")
    for so in native:
        print(f"  {so}")
    if size_mib > QUERY_SIZE_LIMIT_MIB:
        print(f"Vendored tree is {size_mib:.1f} MiB; the build limit is {QUERY_SIZE_LIMIT_MIB} MiB "
              f"(Lambda allows 250 MiB unzipped including layers).", file=sys.stderr)
        return 1

    print("Wheel and extension checks passed.")
    print("Import check skipped for this target: Linux extension modules cannot load on Windows; "
          "GET /health after deploy is the import test.")
    return 0


def main() -> int:
    for build in (build_ingest, build_query):
        code = build()
        if code != 0:
            return code
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
