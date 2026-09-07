"""Put both Lambda code roots on sys.path.

Each service is packaged with its own directory as the code root, so `rag.*` and the
flat ingest modules import in tests exactly as they do in Lambda.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for code_root in (ROOT / "services" / "query", ROOT / "services" / "ingest"):
    path = str(code_root)
    if path not in sys.path:
        sys.path.insert(0, path)
