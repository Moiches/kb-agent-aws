"""Listing and deleting documents.

Three services each ship a module called `handler`, so this one is loaded by path rather
than by name. Importing it as `handler` would resolve to whichever code root happened to sit
first on `sys.path` -- a test that passes or fails based on the order of a loop in conftest.
"""

import datetime as dt
import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "services" / "documents" / "handler.py"
_spec = importlib.util.spec_from_file_location("documents_handler", _PATH)
documents = importlib.util.module_from_spec(_spec)
sys.modules["documents_handler"] = documents
_spec.loader.exec_module(documents)


# ------------------------------------------------------------------------------- fakes


class FakeS3:
    def __init__(self, stored: dict[str, int], indexed: dict[str, int] | None = None):
        self.stored = dict(stored)
        self.indexed = indexed
        self.deleted: list[str] = []
        self.presigned: list[dict] = []

    # -- listing
    def get_paginator(self, _operation):
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 -- boto3 casing
                yield {"Contents": [
                    {"Key": f"{Prefix}{name}", "Size": size,
                     "LastModified": dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)}
                    for name, size in outer.stored.items()
                ]}

        return Paginator()

    # -- index artifact
    def head_object(self, Bucket, Key):  # noqa: N803
        if self.indexed is None:
            raise RuntimeError("NoSuchKey")
        return {"ETag": '"' + str(sorted(self.indexed.items())) + '"'}

    def get_object(self, Bucket, Key):  # noqa: N803
        chunks = [
            {"chunk_id": f"{doc}#chunk-{i}", "document_id": doc}
            for doc, n in (self.indexed or {}).items() for i in range(n)
        ]
        payload = gzip.compress(json.dumps({"kb_version": "2026-09-08T21:00:00+00:00",
                                            "chunks": chunks}).encode())

        class Body:
            def read(self_inner):
                return payload

        return {"Body": Body()}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)
        return {}

    def generate_presigned_post(self, Bucket, Key, Fields, Conditions, ExpiresIn):  # noqa: N803
        self.presigned.append({"Key": Key, "Conditions": Conditions, "ExpiresIn": ExpiresIn})
        return {"url": f"https://{Bucket}.s3.amazonaws.com/",
                "fields": {"key": Key, "policy": "b64", **Fields}}


class FakeLambda:
    def __init__(self, fails: bool = False):
        self.invocations: list[dict] = []
        self.fails = fails

    def invoke(self, **kwargs):
        if self.fails:
            raise RuntimeError("throttled")
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


@pytest.fixture
def wired(monkeypatch):
    """Point the handler at fakes and clear the cache between tests."""
    def _wire(stored, indexed=None, ingest_fails=False, ingest_name="kb-ingest"):
        s3, lam = FakeS3(stored, indexed), FakeLambda(ingest_fails)
        monkeypatch.setattr(documents, "_s3", s3)
        monkeypatch.setattr(documents, "_lambda", lam)
        monkeypatch.setattr(documents, "BUCKET", "test-bucket")
        monkeypatch.setattr(documents, "RAW_PREFIX", "raw/")
        monkeypatch.setattr(documents, "INGEST_FUNCTION_NAME", ingest_name)
        monkeypatch.setattr(documents, "_summary_cache", None)
        return s3, lam
    return _wire


def call(method, resource, path_params=None, body=None):
    return documents.lambda_handler({
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": "req-1"},
    }, None)


def body_of(response):
    return json.loads(response["body"])


# -------------------------------------------------------------------- GET /documents


def test_listing_reports_stored_and_indexed_state(wired):
    wired({"a.md": 100, "b.md": 200}, indexed={"a.md": 3, "b.md": 5})
    result = body_of(call("GET", "/documents"))

    assert [d["document_id"] for d in result["documents"]] == ["a.md", "b.md"]
    assert result["documents"][0]["chunks"] == 3
    assert all(d["indexed"] for d in result["documents"])
    assert result["index"]["in_sync"] is True


def test_an_uploaded_but_unindexed_document_is_named(wired):
    """The bug this endpoint exists to make visible: a file in S3 that nothing indexed."""
    wired({"a.md": 100, "new.pdf": 3_500_000}, indexed={"a.md": 3})
    result = body_of(call("GET", "/documents"))

    assert result["index"]["pending_ingest"] == ["new.pdf"]
    assert result["index"]["in_sync"] is False
    new = next(d for d in result["documents"] if d["document_id"] == "new.pdf")
    assert new["indexed"] is False and new["chunks"] == 0


def test_a_deleted_document_still_in_the_index_is_named(wired):
    """The reverse drift, and the more dangerous one: it is still being answered from."""
    wired({"a.md": 100}, indexed={"a.md": 3, "gone.md": 4})
    result = body_of(call("GET", "/documents"))

    assert result["index"]["orphaned_in_index"] == ["gone.md"]
    assert result["index"]["in_sync"] is False


def test_an_unsupported_file_is_listed_not_hidden(wired):
    """The ingest will skip it. Someone needs to be told that, not shielded from it."""
    wired({"notes.docx": 50}, indexed={})
    result = body_of(call("GET", "/documents"))
    entry = result["documents"][0]
    assert entry["document_id"] == "notes.docx"
    assert entry["supported"] is False


def test_no_index_yet_is_a_state_not_a_crash(wired):
    wired({"a.md": 100}, indexed=None)
    result = body_of(call("GET", "/documents"))
    assert result["index"]["chunk_count"] == 0
    assert result["index"]["pending_ingest"] == ["a.md"]


# ------------------------------------------------------------ DELETE /documents/{id}


def test_delete_removes_the_object_and_starts_a_reindex(wired):
    s3, lam = wired({"a.md": 100, "b.md": 200}, indexed={"a.md": 3, "b.md": 5})
    response = call("DELETE", "/documents/{documentId}", {"documentId": "a.md"})

    assert response["statusCode"] == 202  # accepted: the reindex has not finished
    assert s3.deleted == ["raw/a.md"]
    assert len(lam.invocations) == 1
    assert lam.invocations[0]["InvocationType"] == "Event"

    result = body_of(response)
    assert result["deleted"] == "a.md"
    assert result["remaining_documents"] == 1
    assert result["reindex"] == "started"


def test_deleting_something_that_is_not_there_is_a_404(wired):
    s3, _ = wired({"a.md": 100}, indexed={"a.md": 3})
    response = call("DELETE", "/documents/{documentId}", {"documentId": "ghost.md"})
    assert response["statusCode"] == 404
    assert body_of(response)["error"] == "not_found"
    assert s3.deleted == []


@pytest.mark.parametrize("attack", [
    "../index/kb-index.json.gz",
    "..%2Findex%2Fkb-index.json.gz",
    "../../raw/a.md",
    "raw/a.md",
])
def test_a_path_cannot_escape_the_raw_prefix(wired, attack):
    """The index lives one prefix away from the documents. Building the key by string
    concatenation would put it one `../` from being deleted through a public endpoint."""
    s3, _ = wired({"a.md": 100}, indexed={"a.md": 3})
    response = call("DELETE", "/documents/{documentId}", {"documentId": attack})

    assert response["statusCode"] == 404
    assert s3.deleted == []


def test_an_empty_id_is_rejected(wired):
    s3, _ = wired({"a.md": 100})
    response = call("DELETE", "/documents/{documentId}", {"documentId": "   "})
    assert response["statusCode"] == 400
    assert s3.deleted == []


def test_a_double_encoded_filename_still_resolves(wired):
    """Filenames with spaces are normal, and clients encode them inconsistently."""
    s3, _ = wired({"my paper.pdf": 100}, indexed={})
    response = call("DELETE", "/documents/{documentId}", {"documentId": "my%20paper.pdf"})
    assert response["statusCode"] == 202
    assert s3.deleted == ["raw/my paper.pdf"]


def test_a_failed_reindex_does_not_hide_that_the_delete_happened(wired):
    """The object is already gone. Reporting a 500 would describe a deletion that did
    happen as one that did not, and send someone looking for a document that is not there."""
    s3, _ = wired({"a.md": 100}, indexed={"a.md": 3}, ingest_fails=True)
    response = call("DELETE", "/documents/{documentId}", {"documentId": "a.md"})

    assert response["statusCode"] == 202
    assert s3.deleted == ["raw/a.md"]
    result = body_of(response)
    assert result["reindex"] == "failed"
    assert "still searchable" in result["message"]


def test_reindex_reports_unavailable_when_no_function_is_configured(wired):
    wired({"a.md": 100}, ingest_name="")
    result = body_of(call("DELETE", "/documents/{documentId}", {"documentId": "a.md"}))
    assert result["reindex"] == "unavailable"


# -------------------------------------------------------------------- POST /documents


def test_upload_returns_a_presigned_post_scoped_to_the_raw_prefix(wired):
    s3, _ = wired({"a.md": 100}, indexed={"a.md": 3})
    response = call("POST", "/documents", body={"filename": "handbook.pdf"})

    assert response["statusCode"] == 200
    result = body_of(response)
    assert result["document_id"] == "handbook.pdf"
    assert result["replaces_existing"] is False
    assert result["upload"]["method"] == "POST"
    assert s3.presigned[0]["Key"] == "raw/handbook.pdf"


def test_the_size_limit_is_a_condition_on_the_presigned_post(wired):
    """Enforced by S3, not by the client. A limit the uploader can edit is not a limit."""
    s3, _ = wired({})
    call("POST", "/documents", body={"filename": "handbook.pdf"})

    conditions = s3.presigned[0]["Conditions"]
    ranges = [c for c in conditions if isinstance(c, list) and c[0] == "content-length-range"]
    assert ranges, "no content-length-range condition was attached"
    assert ranges[0][2] == documents.MAX_UPLOAD_BYTES
    assert s3.presigned[0]["ExpiresIn"] == documents.UPLOAD_URL_TTL_SECONDS


def test_replacing_an_existing_document_is_reported(wired):
    wired({"a.md": 100}, indexed={"a.md": 3})
    result = body_of(call("POST", "/documents", body={"filename": "a.md"}))
    assert result["replaces_existing"] is True


@pytest.mark.parametrize("filename", [
    "../index/kb-index.json.gz",
    "..%2Findex%2Fkb-index.json.gz",
    "subdir/file.md",
    ".hidden.md",
])
def test_an_upload_filename_cannot_escape_the_raw_prefix(wired, filename):
    """The delete path resolves an id against a listing. This one cannot -- the file does
    not exist yet -- so validation carries the whole weight."""
    s3, _ = wired({})
    response = call("POST", "/documents", body={"filename": filename})

    assert response["statusCode"] == 400
    assert s3.presigned == []


@pytest.mark.parametrize("filename", ["notes.docx", "archive.zip", "script.py", "noextension"])
def test_a_type_the_ingest_cannot_read_is_refused(wired, filename):
    """Accepting it would store a document that never becomes searchable -- the exact
    silent failure this whole API was built to end."""
    s3, _ = wired({})
    response = call("POST", "/documents", body={"filename": filename})

    assert response["statusCode"] == 400
    assert "Unsupported file type" in body_of(response)["message"]
    assert s3.presigned == []


def test_a_missing_filename_is_rejected(wired):
    s3, _ = wired({})
    assert call("POST", "/documents", body={})["statusCode"] == 400
    assert s3.presigned == []


def test_an_overlong_filename_is_rejected(wired):
    s3, _ = wired({})
    assert call("POST", "/documents", body={"filename": "x" * 201 + ".md"})["statusCode"] == 400
    assert s3.presigned == []


def test_a_filename_with_spaces_is_accepted(wired):
    """Real documents have spaces in their names; the corpus already contains one."""
    s3, _ = wired({})
    response = call("POST", "/documents", body={"filename": "my annual report.pdf"})
    assert response["statusCode"] == 200
    assert s3.presigned[0]["Key"] == "raw/my annual report.pdf"


def test_a_body_that_is_not_json_is_rejected(wired):
    s3, _ = wired({})
    response = documents.lambda_handler(
        {"httpMethod": "POST", "resource": "/documents", "body": "not json",
         "requestContext": {"requestId": "req-1"}}, None)
    assert response["statusCode"] == 400
    assert s3.presigned == []


# ------------------------------------------------------------------------- envelope


def test_an_unknown_route_uses_the_shared_error_envelope(wired):
    wired({})
    response = call("PUT", "/documents")
    assert response["statusCode"] == 404
    result = body_of(response)
    assert set(result) == {"error", "message", "request_id"}
    assert result["request_id"] == "req-1"
