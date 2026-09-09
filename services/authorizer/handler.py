"""API Gateway TOKEN authorizer.

Validates `Authorization: Bearer <token>` against a secret held in Secrets Manager, and
returns an IAM policy allowing or denying the call (ADR-04).

Three layers reject an unauthorised request, and only the second one runs code:

1. API Gateway checks the token against `validationRegex` first. A malformed value is
   rejected as 401 **without invoking this function**, so a flood of junk requests costs
   nothing and cannot be used to run up a bill.
2. This function compares the token with `hmac.compare_digest`. Constant time: `==` leaks
   how many leading characters matched, which is enough to reconstruct a secret given
   enough attempts.
3. The returned policy is cached by API Gateway for five minutes per token, so a busy
   client pays for this once rather than on every call.

The token itself is never logged. Only a fingerprint -- the first eight characters of its
SHA-256 -- which is enough to correlate requests without disclosing the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time

import boto3

SECRET_ARN = os.environ.get("API_TOKEN_SECRET_ARN", "")
SECRET_TTL_SECONDS = int(os.environ.get("API_TOKEN_TTL_SECONDS", "300"))

_secrets = boto3.client("secretsmanager")

# Cached across warm invocations so rotation takes effect without a redeploy, and so the
# common path does not call Secrets Manager at all.
_token: str = ""
_token_read_at: float = 0.0

# Grants every method of the stage rather than just this one, so the cached policy is
# reusable. Scoping to a single method would make API Gateway re-invoke the authorizer for
# each distinct path, which costs latency and money for no security benefit: the token
# carries no per-route authority anyway.
_METHOD_ARN = re.compile(r"^(arn:aws[\w-]*:execute-api:[^:]+:\d+:[^/]+/[^/]+)/")


def lambda_handler(event, context):  # noqa: ARG001
    method_arn = event.get("methodArn", "")
    presented = _strip_bearer(event.get("authorizationToken", ""))
    expected = _expected_token()

    if not expected:
        # Misconfiguration, not an authorisation decision. Denying is the safe outcome, but
        # it must be distinguishable in the logs from a genuine bad token.
        _log("authorizer_misconfigured", detail="API token secret is empty or unreadable")
        raise Exception("Unauthorized")  # noqa: TRY002 -- API Gateway matches on this string

    authorized = hmac.compare_digest(presented, expected)
    _log(
        "authorization_checked",
        authorized=authorized,
        token_fingerprint=_fingerprint(presented),
        source_ip=(event.get("requestContext") or {}).get("identity", {}).get("sourceIp"),
    )

    if not authorized:
        # An explicit Deny produces 403 with our custom gateway response. Raising
        # "Unauthorized" instead would produce 401, which would wrongly imply no credential
        # was presented when in fact a wrong one was.
        return _policy("unauthenticated", "Deny", method_arn, presented)

    return _policy("kb-agent-client", "Allow", method_arn, presented)


def _policy(principal_id: str, effect: str, method_arn: str, token: str) -> dict:
    match = _METHOD_ARN.match(method_arn)
    resource = f"{match.group(1)}/*/*" if match else method_arn

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": resource}],
        },
        # Reaches the access log as $context.authorizer.*, which is what ties a request in
        # the gateway log to a caller without ever writing the token down.
        "context": {
            "principalId": principal_id,
            "tokenFingerprint": _fingerprint(token),
        },
    }


def _expected_token() -> str:
    global _token, _token_read_at

    if _token and (time.time() - _token_read_at) < SECRET_TTL_SECONDS:
        return _token
    if not SECRET_ARN:
        return ""

    try:
        value = _secrets.get_secret_value(SecretId=SECRET_ARN)
    except Exception as exc:  # noqa: BLE001
        _log("secret_unreadable", error=type(exc).__name__)
        return ""

    _token = (value.get("SecretString") or "").strip()
    _token_read_at = time.time()
    return _token


def _strip_bearer(header: str) -> str:
    header = (header or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8] if token else ""


def _log(message: str, **fields) -> None:
    print(json.dumps({"service": "kb-agent-authorizer", "message": message, **fields}, default=str))
