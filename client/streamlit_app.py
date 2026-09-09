"""Local Streamlit client for the AWS-hosted Knowledge Base Agent.

Runs on the reviewer's machine and talks to the deployed API over HTTPS with a bearer
token. It is deliberately thin: it makes HTTP calls and renders the response. It holds no
AWS credentials, no provider key, no retrieval logic and no prompt. Every decision that
matters -- what to retrieve, how to ground the answer, how confident to be -- happens in
the Lambda, where it can be logged, permissioned and tested.

That split is the central change from the reference prototype, where a single Streamlit
process did the embedding, the vector search, the generation and the UI, and needed the
model provider's API key on the laptop to do it.

    pip install -r client/requirements.txt
    python scripts/configure_client.py          # writes .streamlit/secrets.toml
    streamlit run client/streamlit_app.py
"""

from __future__ import annotations

import json
import os
import uuid
from urllib.parse import quote

import requests
import streamlit as st

REQUEST_TIMEOUT = 45  # the API itself caps at 29 s; this only has to be larger
UPLOAD_TIMEOUT = 300  # a large PDF over a domestic uplink, not an API call

# Grounding label -> (badge colour, plain-language meaning). The number alone tells a user
# nothing; "verify against the sources" tells them what to do.
GROUNDING_STYLE = {
    "high": ("🟢", "Well grounded in the retrieved sources."),
    "medium": ("🟡", "Grounded, but worth checking the sources below."),
    "low": ("🔴", "Weakly grounded — verify against the sources before relying on this."),
    "insufficient_context": ("⚪", "Not answerable from the knowledge base."),
}

# Every failure the API can return, mapped to something a human can act on. A client that
# renders a raw 503 has pushed its problem onto the user.
ERROR_HELP = {
    "unauthorized": "No token was sent, or it is malformed. Check `API_TOKEN` in `.streamlit/secrets.toml`.",
    "forbidden": "The token was rejected. Re-read it from Secrets Manager — it may have been rotated.",
    "bad_request": "The request was rejected as invalid.",
    "not_found": "That route does not exist. Check `API_BASE_URL` — it should end with the stage name.",
    "rate_limited": "Rate limited by the API gateway. Wait a few seconds and retry.",
    "kb_unavailable": "The knowledge base has not been seeded. Run the seed command from the stack outputs.",
    "model_access_denied": "The API could not authenticate to the model provider. Check the provider API key secret.",
    "model_throttled": "The model provider is rate limiting. Retry shortly.",
    "model_timeout": "The model provider timed out. Retry, or lower `top_k`.",
    "model_unavailable": "The model provider is unavailable. Retry shortly.",
    "internal_error": "The service hit an unexpected error. The request id below is what to search for in the logs.",
}


# --------------------------------------------------------------------------- config


def read_config() -> tuple[str, str]:
    """Streamlit secrets, then environment, then the sidebar.

    The sidebar fallback exists so a live demo can be recovered without editing a file.
    """
    base_url, token = "", ""
    try:
        base_url = st.secrets.get("API_BASE_URL", "")
        token = st.secrets.get("API_TOKEN", "")
    except Exception:  # noqa: BLE001 -- no secrets.toml at all is a normal first run
        pass
    return (
        os.environ.get("API_BASE_URL") or base_url,
        os.environ.get("API_TOKEN") or token,
    )


def call_api(base_url: str, token: str, path: str, payload: dict | None = None,
             method: str | None = None) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    verb = method or ("POST" if payload is not None else "GET")
    try:
        response = requests.request(
            verb, url, headers=headers,
            json=payload if payload is not None else None,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        return 0, {"error": "client_timeout", "message": f"No response within {REQUEST_TIMEOUT}s."}
    except requests.RequestException as exc:
        return 0, {"error": "client_network", "message": str(exc)}

    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": "client_bad_response", "message": response.text[:400]}


# --------------------------------------------------------------------------- rendering


def render_answer(result: dict) -> None:
    st.markdown(result.get("answer", ""))

    grounding = result.get("grounding", "low")
    icon, meaning = GROUNDING_STYLE.get(grounding, ("⚪", ""))
    metadata = result.get("metadata", {})

    left, middle, right = st.columns([2, 1, 1])
    left.markdown(f"{icon} **{grounding.replace('_', ' ').title()}** — {meaning}")
    middle.metric("Confidence", f"{result.get('confidence', 0):.2f}")
    right.metric("Latency", f"{metadata.get('latency_ms', 0) / 1000:.1f} s")

    render_sources(result.get("sources", []), abstained=grounding == "insufficient_context")
    render_debug(result)


def as_plain_text(text: str) -> str:
    """Render a document excerpt verbatim.

    Excerpts are raw Markdown lifted out of the source documents, so Streamlit happily
    renders `## Service credits` as a heading and a pipe table as a table. That misrepresents
    what the retriever actually matched on, and it wrecks the layout. Escaping the handful of
    characters Markdown reacts to keeps the excerpt looking like what it is: source text.
    """
    for char in ("\\", "`", "*", "_", "#", "|", "[", "]", "<", ">"):
        text = text.replace(char, "\\" + char)
    return text


def render_sources(sources: list[dict], abstained: bool = False) -> None:
    if not sources:
        st.caption(
            "No passage scored above the relevance floor — nothing in the knowledge base "
            "came close enough to be worth reading."
            if abstained
            else "No sources — the knowledge base did not contain a relevant passage."
        )
        return

    if abstained:
        # These were retrieved and then rejected. Labelling them as evidence would be wrong,
        # but hiding them is worse: they usually show the right document was found and the
        # wrong section of it, which is a different problem from the document being absent.
        header = f"Retrieved but judged insufficient — {len(sources)} passages"
    else:
        cited = sum(1 for s in sources if s.get("cited"))
        header = f"Sources — {cited} of {len(sources)} retrieved passages were cited"

    with st.expander(header, expanded=True):
        if abstained:
            st.caption(
                "The model read these and reported they do not answer the question. "
                "They are shown so you can judge that for yourself — not as support for "
                "the answer above."
            )
        for source in sources:
            where = source.get("section") or (f"page {source['page']}" if source.get("page") else "")
            title = source.get("document_title") or source.get("document_id")
            mark = ("rejected" if abstained
                    else "✓ cited" if source.get("cited")
                    else "retrieved, not cited")

            st.markdown(
                f"**{title}**{f' › {where}' if where else ''}  \n"
                f"`{source['chunk_id']}` · score `{source['score']:.3f}` · _{mark}_"
            )
            st.caption(as_plain_text(source.get("excerpt", "")))
            st.divider()


# Only a fallback for the size check below, used if the API's response somehow omits the
# real figure. The authoritative limit is the `content-length-range` S3 enforces.
MAX_UPLOAD_HINT = 20 * 1024 * 1024

FILE_ICONS = {".pdf": "📕", ".md": "📄", ".markdown": "📄", ".txt": "📝"}


def human_size(num_bytes: int) -> str:
    for unit, cutoff in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num_bytes >= cutoff:
            return f"{num_bytes / cutoff:.2f} {unit}"
    return f"{num_bytes} B"


def render_status(base_url: str, token: str) -> tuple[dict, "st.delta_generator.DeltaGenerator | None"]:
    """Health as a banner and three numbers, and the numbers are chosen deliberately.

    `Documents` and `Passages` are both here because they answer different questions and
    people conflate them. Documents is what you uploaded; passages is what retrieval can
    actually reach, and a document contributing few passages is the shape of a file that
    parsed badly. One number without the other hides that.

    Everything comes from `/health`, so the banner reports what the query Lambda has
    loaded -- not what is sitting in the bucket. Those differ for about a minute after any
    change, and this panel is the wrong place to blur them.
    """
    st.subheader("Status")
    if not base_url or not token:
        st.warning("Not configured", icon="⚠️")
        return {}, None

    status, health = call_api(base_url, token, "/health")
    if status != 200:
        st.error("API unreachable", icon="🚫")
        st.caption(health.get("message", f"HTTP {status}"))
        return {}, None

    # `kb_loaded` only says the artifact parsed. An index that loaded perfectly and holds
    # nothing is not an active knowledge base -- it cannot answer a single question -- and
    # calling it active is the kind of green badge that sends someone debugging the model
    # when the real problem is an empty corpus.
    if health.get("kb_loaded") and health.get("chunk_count", 0) > 0:
        st.success("Knowledge Base Active", icon="✅")
    elif health.get("kb_loaded"):
        st.warning("Knowledge Base Empty", icon="⚠️")
        st.caption("The index loaded and contains no passages. Upload a document, or restore "
                   "the samples with `aws s3 cp sample-docs/ s3://<bucket>/raw/ --recursive`.")
    else:
        # Deployed but never seeded. A real state, and the one a reviewer meets between
        # `cdk deploy` and the first ingest -- reporting it as an error would send someone
        # debugging a system that is working exactly as designed.
        st.warning("Knowledge Base Not Seeded", icon="⚠️")
        st.caption(health.get("detail", ""))

    documents_column, passages_column = st.columns(2)
    documents_column.metric("Documents", health.get("document_count", 0))
    passages_column.metric("Passages", health.get("chunk_count", 0))
    # "Questions" rather than the messages a chat UI would count: one entry here is a
    # question and its answer, and calling that two messages would inflate a number people
    # read as work done.
    #
    # Held in a placeholder because the sidebar renders before the question at the bottom of
    # the script is answered. Without it the count is always one behind -- ask the first
    # question, watch it say zero. Rewriting the slot afterwards costs nothing; a full
    # `st.rerun()` would repeat the /health and /documents calls on every question.
    questions_slot = st.empty()
    questions_slot.metric("Questions", len(st.session_state.history))

    version = health.get("kb_version") or ""
    if version:
        st.caption(f"Index `{version[:19]}`")
    st.caption(f"Provider `{health.get('provider')}` · `{health.get('model')}`")
    return health, questions_slot


def render_upload(base_url: str, token: str) -> None:
    """Upload a document straight to S3, using a URL the API signs.

    The bytes never pass through the API. That is not an optimisation: API Gateway caps a
    request body at 10 MB, which a single real PDF exceeds, so any design that proxies the
    file has a ceiling built into it. The API grants permission and S3 takes the upload.

    Nothing here has to trigger the ingest afterwards. S3 emits an event on the new object
    and the index rebuilds on its own -- which is also what makes a file dropped in through
    the console behave the same way as one uploaded here.
    """
    st.subheader("Browse Files")
    if not base_url or not token:
        st.caption("Not connected.")
        return

    chosen = st.file_uploader(
        "Upload Documents",
        type=["pdf", "md", "markdown", "txt"],
        help="The ingest reads PDF, Markdown and plain text. Other formats are refused "
             "rather than stored, because a document that can never be indexed is worse "
             "than one that was never uploaded.",
    )
    # No size caption here on purpose. Streamlit prints its own from `server.maxUploadSize`,
    # which `npm run client` sets to match; a second line would only be a chance for the two
    # to disagree, and the authoritative limit is neither of them -- it is the signed
    # condition S3 enforces.

    if chosen is None:
        return

    signature = (chosen.name, chosen.size)
    if st.session_state.get("uploaded_signature") == signature:
        st.success(f"**{chosen.name}** uploaded.")
        st.caption(
            "Reindexing starts automatically and takes about a minute to affect answers. "
            "Clear the file above to upload another."
        )
        return

    st.caption(f"Ready: **{chosen.name}** · {human_size(chosen.size)}")
    if not st.button("Upload", type="primary", use_container_width=True):
        return

    status, grant = call_api(base_url, token, "/documents", payload={"filename": chosen.name})
    if status != 200:
        st.error(f"Upload refused (HTTP {status})")
        st.caption(grant.get("message", ""))
        return

    if chosen.size > grant.get("max_bytes", MAX_UPLOAD_HINT):
        # S3 would reject this too -- the size is a signed condition, not a client-side
        # courtesy. Catching it here just turns a 400 from S3 into a sentence.
        st.error(f"Too large: {human_size(chosen.size)}, limit is {human_size(grant['max_bytes'])}.")
        return

    upload = grant["upload"]
    try:
        response = requests.post(
            upload["url"],
            data=upload["fields"],
            files={"file": (chosen.name, chosen.getvalue(),
                            upload["fields"].get("Content-Type", "application/octet-stream"))},
            timeout=UPLOAD_TIMEOUT,
        )
    except requests.RequestException as exc:
        st.error("Upload failed.")
        st.caption(str(exc))
        return

    if response.status_code not in (200, 201, 204):
        st.error(f"S3 rejected the upload (HTTP {response.status_code})")
        st.caption(response.text[:300])
        return

    st.session_state.uploaded_signature = signature
    st.rerun()


def render_knowledge_base(base_url: str, token: str, serving_version: str = "") -> None:
    """The corpus, with what is searchable made visible.

    The prototype this replaces listed filenames. This lists filenames *and whether the
    system can actually answer from them*, because those are different facts: a file sits in
    S3 the moment it is uploaded, and stays unsearchable until the ingest runs. Showing only
    the first would reproduce the exact confusion the `GET /documents` endpoint was built to
    end -- a document visibly present and silently absent from every answer.

    `serving_version` is `/health`'s `kb_version`: the index the query Lambda currently holds
    in memory. It is compared against the artifact's version because they are not the same
    thing and the gap is not small. Measured on a delete: the artifact was rebuilt in 6
    seconds and the query path went on answering from the old one for 54 more, because a warm
    execution environment re-checks the artifact at most once a minute. `in_sync` alone would
    have said "done" for most of the window in which the answer was still wrong.
    """
    st.subheader("Knowledge Base Files")
    if not base_url or not token:
        st.caption("Not connected.")
        return

    status, body = call_api(base_url, token, "/documents")
    if status != 200:
        st.error(f"Could not list documents (HTTP {status})")
        st.caption(body.get("message", ""))
        return

    documents, index = body.get("documents", []), body.get("index", {})
    if not documents:
        st.caption("No documents in the knowledge base.")
        return

    if not index.get("in_sync", True):
        pending, orphaned = index.get("pending_ingest", []), index.get("orphaned_in_index", [])
        st.warning("Stored and searchable have drifted apart.")
        if pending:
            st.caption(f"Uploaded but not indexed: {', '.join(pending)}. Run `npm run seed`.")
        if orphaned:
            # The dangerous direction: answers can still cite a document that is gone.
            st.caption(f"Deleted but still searchable: {', '.join(orphaned)}. Reindexing may be running.")
    elif serving_version and index.get("kb_version") and serving_version != index["kb_version"]:
        # The index file is correct and the answers are not yet. This is the state the
        # `in_sync` flag cannot see, and it lasts up to a minute after every reindex.
        st.info("Index rebuilt. The query path is still serving the previous one.")
        st.caption(
            f"Answering from `{serving_version[:19]}`, latest is `{index['kb_version'][:19]}`. "
            "A warm Lambda re-checks the index at most once a minute, so this clears within 60s."
        )

    for document in documents:
        document_id = document["document_id"]
        extension = "." + document_id.rsplit(".", 1)[-1].lower() if "." in document_id else ""
        icon = FILE_ICONS.get(extension, "📄")

        name_column, action_column = st.columns([5, 1], vertical_alignment="center")
        with name_column:
            st.markdown(f"{icon} **{document_id}**")
            detail = human_size(document["size_bytes"])
            if not document.get("supported", True):
                detail += " · unsupported type, not indexed"
            elif document.get("indexed"):
                detail += f" · {document['chunks']} passages"
            else:
                detail += " · not indexed yet"
            st.caption(detail)

        with action_column:
            if st.session_state.get("pending_delete") != document_id:
                if st.button("🗑", key=f"delete-{document_id}", help="Delete this document"):
                    # Two steps on purpose. This deletes the object from S3 and rebuilds the
                    # index; a stray click in a sidebar should not be able to do that.
                    st.session_state.pending_delete = document_id
                    st.rerun()

        if st.session_state.get("pending_delete") == document_id:
            st.warning(f"Delete **{document_id}** and reindex?")
            confirm_column, cancel_column = st.columns(2)
            if confirm_column.button("Delete", key=f"confirm-{document_id}",
                                     type="primary", use_container_width=True):
                delete_status, delete_body = call_api(
                    base_url, token, f"/documents/{quote(document_id, safe='')}", method="DELETE"
                )
                st.session_state.pending_delete = None
                st.session_state.delete_flash = (delete_status, delete_body)
                st.rerun()
            if cancel_column.button("Cancel", key=f"cancel-{document_id}", use_container_width=True):
                st.session_state.pending_delete = None
                st.rerun()

        st.divider()

    flash = st.session_state.pop("delete_flash", None)
    if flash:
        flash_status, flash_body = flash
        if flash_status == 202:
            st.success(f"Deleted **{flash_body.get('deleted')}**.")
            # Saying "done" here would be a lie for the next half minute.
            st.caption(
                "Reindexing runs in the background; until it finishes the document is still "
                "searchable. Reload to watch it disappear."
                if flash_body.get("reindex") == "started" else
                "Reindexing could not be started -- run `npm run seed` to rebuild the index."
            )
        else:
            st.error(f"Delete failed (HTTP {flash_status})")
            st.caption(flash_body.get("message", ""))

    st.caption(
        "Documents are added by uploading to the bucket's `raw/` prefix and running the "
        "ingest. There is no upload endpoint: API Gateway caps a body at 10 MB, so uploads "
        "belong on a presigned S3 URL."
    )


def render_debug(result: dict) -> None:
    """Everything needed to chase a request into CloudWatch, in the UI.

    This is what makes the system debuggable from the client: a user reporting a bad answer
    can hand over one request id, and the Logs Insights query below reconstructs the run.
    """
    metadata = result.get("metadata", {})
    request_id = metadata.get("request_id", "")

    with st.expander("Debug"):
        components = metadata.get("confidence_components", {})
        st.markdown(
            f"**Request id** `{request_id}`  \n"
            f"**Provider** `{metadata.get('provider')}` · **Model** `{metadata.get('model')}`  \n"
            f"**KB version** `{metadata.get('kb_version')}` · searched "
            f"`{metadata.get('chunks_searched')}` chunks  \n"
            f"**Latency** retrieval `{metadata.get('retrieval_ms')} ms` · generation "
            f"`{metadata.get('generation_ms')} ms`  \n"
            f"**Tokens** `{metadata.get('input_tokens')}` in / `{metadata.get('output_tokens')}` out "
            f"· est. `${metadata.get('estimated_cost_usd', 0):.6f}`  \n"
            f"**Confidence** = 0.50·strength `{components.get('retrieval_strength')}` "
            f"+ 0.25·consensus `{components.get('consensus')}` "
            f"+ 0.25·citation coverage `{components.get('citation_coverage')}`"
        )
        if metadata.get("dropped_citations"):
            st.warning(
                f"Citations the model invented and the API removed: "
                f"`{', '.join(metadata['dropped_citations'])}`"
            )

        st.caption("Find this request in CloudWatch Logs Insights:")
        st.code(
            "fields @timestamp, message, confidence, top_score, latency_ms\n"
            f'| filter correlation_id = "{request_id}"\n'
            "| sort @timestamp asc",
            language="text",
        )
        st.caption("Raw response")
        st.json(result, expanded=False)


def render_error(status: int, body: dict) -> None:
    error = body.get("error", "unknown")
    st.error(f"**{error}** (HTTP {status}) — {body.get('message', '')}")

    if error in ERROR_HELP:
        st.info(ERROR_HELP[error])
    if body.get("hint"):
        st.info(body["hint"])
    if body.get("request_id"):
        st.caption(f"Request id: `{body['request_id']}` — quote this when reporting the problem.")


# ------------------------------------------------------------------------------- app


st.set_page_config(page_title="Knowledge Base Agent", page_icon="📚", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []
if "session_id" not in st.session_state:
    # One id per browser session, so a conversation can be reconstructed from the query log.
    st.session_state.session_id = str(uuid.uuid4())

base_url, token = read_config()

with st.sidebar:
    if not base_url or not token:
        # Only shown when there is nothing to connect with. The fallback exists so a live
        # demo can be recovered without editing a file.
        st.subheader("Connection")
        st.caption("Run `python scripts/configure_client.py`, or paste the values below.")
        base_url = st.text_input("API base URL", value=base_url)
        token = st.text_input("API token", value=token, type="password")

    health, questions_slot = render_status(base_url, token)

    st.subheader("Settings")
    top_k = st.slider("Passages to retrieve (top_k)", 1, 10, 5)
    simple = st.toggle(
        "Explain like I'm 10",
        value=False,
        help="Plain language for someone new to the subject. Citations and the refusal to "
             "answer beyond the documents are unchanged -- only the wording gets simpler.",
    )
    st.caption(f"Session `{st.session_state.session_id[:8]}`")

    if st.button("Clear history", use_container_width=True):
        st.session_state.history = []
        st.rerun()

    render_upload(base_url, token)
    render_knowledge_base(base_url, token, health.get("kb_version", "") if isinstance(health, dict) else "")

    st.subheader("Try these")
    st.caption(
        "**Answerable**\n"
        "- How long does an Enterprise customer have to request a refund?\n"
        "- What service credit applies if uptime drops to 99.0%?\n"
        "- Who do I page if the on-call engineer does not respond?\n\n"
        "**Should abstain** — nothing in the knowledge base covers these\n"
        "- Do you sign HIPAA Business Associate Agreements?\n"
        "- What is your policy on cryptocurrency payments?"
    )

st.title("Knowledge Base Agent")
st.caption(
    "Retrieval-augmented answers grounded in a small corpus of business documents. "
    "Every claim carries a citation that the API verifies against what was actually retrieved."
)

for entry in st.session_state.history:
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        if entry.get("error"):
            render_error(entry["status"], entry["body"])
        else:
            render_answer(entry["body"])

question = st.chat_input("Ask a question about the knowledge base")

if question:
    if not base_url or not token:
        st.error("Configure the API base URL and token in the sidebar first.")
        st.stop()

    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Retrieving and generating…"):
            status, body = call_api(
                base_url, token, "/query",
                {
                    "question": question,
                    "session_id": st.session_state.session_id,
                    "top_k": top_k,
                    "style": "simple" if simple else "standard",
                },
            )
        if status == 200:
            render_answer(body)
        else:
            render_error(status, body)

    st.session_state.history.append(
        {"question": question, "status": status, "body": body, "error": status != 200}
    )
    if questions_slot is not None:
        questions_slot.metric("Questions", len(st.session_state.history))
