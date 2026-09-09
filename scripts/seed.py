"""Build the knowledge base index by invoking the ingest Lambda.

    npm run seed
    python scripts/seed.py --stack kbagent-mc-dev

Needed as a separate step because of an ordering constraint that cannot be designed away:
CDK creates the provider API key secret with a placeholder, and the deploy-time trigger runs
in that same deployment, so the first `cdk deploy` has no key to embed with. The trigger
detects that and exits cleanly rather than failing the stack. Once the secret holds a real
key, this rebuilds the index.

    1. npm run deploy
    2. aws secretsmanager put-secret-value --secret-id <prefix>-<env>/provider-api-key \
         --secret-string sk-or-...
    3. npm run seed
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WINDOWS_CLI = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")


def aws_binary() -> str:
    found = shutil.which("aws")
    if found:
        return found
    if WINDOWS_CLI.exists():
        return str(WINDOWS_CLI)
    sys.exit(
        "AWS CLI not found. Install it, or open a new terminal if you installed it in this "
        "one -- a shell keeps the PATH it started with."
    )


def run(args: list[str], profile: str | None) -> str:
    command = [aws_binary(), *args] + (["--profile", profile] if profile else [])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Command failed: {' '.join(args[:3])}\n{result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack", default="kbagent-dev", help="CloudFormation stack name")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    args = parser.parse_args()

    function_name = f"{args.stack}-ingest"
    print(f"Invoking {function_name} ...")

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "seed.json"
        run(
            ["lambda", "invoke", "--function-name", function_name, "--payload", "{}",
             "--cli-binary-format", "raw-in-base64-out", str(output)],
            args.profile,
        )
        result = json.loads(output.read_text(encoding="utf-8"))

    if result.get("seeded"):
        print(
            f"Seeded {result['chunks']} chunks from {result['documents']} documents\n"
            f"  kb_version {result['kb_version']}"
        )
        return 0

    # The two failure modes are different problems with different fixes, so they get
    # different messages rather than a generic "seeding failed".
    reason = result.get("reason", "unknown")
    print(f"Not seeded: {reason}", file=sys.stderr)
    if reason == "no_api_key":
        print(
            f"\nThe provider API key secret still holds its placeholder. Set it with:\n"
            f"  aws secretsmanager put-secret-value \\\n"
            f"    --secret-id {args.stack}/provider-api-key \\\n"
            f"    --secret-string 'sk-or-...'\n"
            f"then run this again.",
            file=sys.stderr,
        )
    elif reason == "no_documents":
        print("\nNo supported documents under raw/. Did `cdk deploy` upload sample-docs/?", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
