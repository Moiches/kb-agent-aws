"""Local Streamlit client for the AWS-hosted Knowledge Base Agent.

Runs on the reviewer's machine and talks to the deployed API over HTTPS with a bearer
token. It is deliberately thin: it makes HTTP calls and renders the response. It holds no
AWS credentials, no provider key, no retrieval logic and no prompt. Every decision that
matters -- what to retrieve, how to ground the answer, how confident to be -- happens in
the Lambda, where it can be logged, permissioned and tested.

That split is the central change from the reference prototype, where a single Streamlit
process did the embedding, the vector search, the generation and the UI, and needed the
model provider's API key on the laptop to do it.

The layout is two panels: the corpus on the left, the conversation on the right. It is not
decoration. The recurring failure in this project was a document that was visibly present
and silently unsearchable, and every version of that bug was invisible from a chat window.
Putting the corpus permanently beside the answers means the question "why did it not use my
document?" has its answer on the same screen.

    pip install -r client/requirements.txt
    python scripts/configure_client.py          # writes .streamlit/secrets.toml
    npm run client
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import requests
import streamlit as st
import theme
from theme import esc, human_size, note, pill, relative_time, section

REQUEST_TIMEOUT = 45   # the API itself caps at 29 s; this only has to be larger
UPLOAD_TIMEOUT = 300   # a large PDF over a domestic uplink, not an API call

# Only a fallback, if the API's response somehow omits the real figure. The authoritative
# limit is the `content-length-range` condition S3 enforces on the presigned POST.
MAX_UPLOAD_HINT = 20 * 1024 * 1024

# Grounding label -> (pill tone, plain-language meaning). The number alone tells a user
# nothing; "verify against the sources" tells them what to do.
GROUNDING = {
    "high": ("ok", "Well grounded", "Every claim is supported by the passages below."),
    "medium": ("info", "Grounded", "Supported, but worth checking the sources."),
    "low": ("warn", "Weakly grounded", "Verify against the sources before relying on this."),
    "insufficient_context": ("warn", "Insufficient context",
                             "Not answerable from the knowledge base."),
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

FILE_KIND = {".pdf": "PDF", ".md": "MD", ".markdown": "MD", ".txt": "TXT"}


# --------------------------------------------------------------------------- transport


def read_config() -> tuple[str, str]:
    """Streamlit secrets, then environment, then the on-screen fallback."""
    base_url, token = "", ""
    try:
        base_url = st.secrets.get("API_BASE_URL", "")
        token = st.secrets.get("API_TOKEN", "")
    except Exception:  # noqa: BLE001 -- no secrets.toml at all is a normal first run
        pass
    return os.environ.get("API_BASE_URL") or base_url, os.environ.get("API_TOKEN") or token


def call_api(base_url: str, token: str, path: str, payload: dict | None = None,
             method: str | None = None) -> tuple[int, dict]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    verb = method or ("POST" if payload is not None else "GET")
    try:
        response = requests.request(verb, url, headers=headers,
                                    json=payload if payload is not None else None,
                                    timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        return 0, {"error": "client_timeout", "message": f"No response within {REQUEST_TIMEOUT}s."}
    except requests.RequestException as exc:
        return 0, {"error": "client_network", "message": str(exc)}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": "client_bad_response", "message": response.text[:400]}


# ------------------------------------------------------------------- document rendering


def document_status(document: dict) -> tuple[str, str]:
    """The status column, mapped from the two facts the API reports about a document.

    `Unsupported` is a status rather than a filter. The ingest will skip such a file, and
    hiding it would reproduce the failure this panel exists to prevent: a document sitting
    in the bucket that no answer will ever use, with nothing on screen to say so.
    """
    if not document.get("supported", True):
        return "Unsupported", "danger"
    if not document.get("indexed"):
        return "Indexing", "info"
    return "Active", "ok"


def render_recent(documents: list[dict]) -> None:
    """The four most recently changed documents, as cards."""
    recent = sorted(documents, key=lambda d: d.get("last_modified", ""), reverse=True)[:4]
    if not recent:
        return

    section("Recent Documents")
    cards = []
    for document in recent:
        extension = "." + document["document_id"].rsplit(".", 1)[-1].lower() \
            if "." in document["document_id"] else ""
        cards.append(f"""
          <div class="kb-card">
            <div class="kb-card-preview">
              <div class="kb-card-badge">{esc(FILE_KIND.get(extension, "FILE"))}</div>
              <div class="kb-card-sheet">
                <div class="kb-card-line w1"></div><div class="kb-card-line w2"></div>
                <div class="kb-card-line w3"></div><div class="kb-card-line w4"></div>
                <div class="kb-card-line w2"></div><div class="kb-card-line w3"></div>
              </div>
            </div>
            <div class="kb-card-body">
              <div class="kb-card-name">{esc(document["document_id"])}</div>
              <div class="kb-card-meta">Last update: {esc(relative_time(document.get("last_modified", "")))}</div>
            </div>
          </div>""")
    st.markdown(f'<div class="kb-cards">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_list(base_url: str, token: str, documents: list[dict]) -> None:
    """The corpus, one row per document, with a real button on each.

    A list rather than the wide table the reference uses, because this lives in a 400px
    sidebar: six columns there would be six columns of truncation. The same facts survive
    the narrower form -- name, type, size, passages, age, status -- stacked instead of
    ranged across.

    Streamlit allows one level of column nesting, which each row spends. That is why the
    delete confirmation is a popover and not a second row of columns: the second level
    would not render at all.
    """
    section(f"All documents \u00b7 {len(documents)}")
    if not documents:
        st.markdown('<div class="kb-empty">No documents yet. Add one to get started.</div>',
                    unsafe_allow_html=True)
        return

    for document in sorted(documents, key=lambda d: d["document_id"].lower()):
        document_id = document["document_id"]
        extension = "." + document_id.rsplit(".", 1)[-1].lower() if "." in document_id else ""
        label, tone = document_status(document)
        passages = document.get("chunks", 0)

        body, action = st.columns([6, 1], vertical_alignment="center")
        body.markdown(
            f'<div class="kb-row-name">{esc(document_id)}</div>'
            f'<div class="kb-row-meta">{esc(FILE_KIND.get(extension, "FILE"))} \u00b7 '
            f'{esc(human_size(document["size_bytes"]))} \u00b7 '
            f'{passages if passages else "no"} passages \u00b7 '
            f'{esc(relative_time(document.get("last_modified", "")))}</div>'
            f'<div style="margin-top:.3rem">{pill(label, tone)}</div>',
            unsafe_allow_html=True)

        with action, st.popover("\U0001F5D1", help=f"Delete {document_id}"):
            st.markdown(f"**Delete {esc(document_id)}?**")
            st.caption("The index rebuilds automatically; the document stays searchable for "
                       "about a minute afterwards.")
            if st.button("Delete permanently", key=f"del-{document_id}", type="primary",
                         use_container_width=True):
                status_code, response = call_api(
                    base_url, token, f"/documents/{quote(document_id, safe='')}", method="DELETE")
                st.session_state.flash = ("delete", status_code, response)
                st.rerun()
        st.markdown("<hr>", unsafe_allow_html=True)


def render_upload(base_url: str, token: str) -> None:
    """Upload straight to S3, with a URL the API signs.

    The bytes never pass through the API, and that is structural rather than an
    optimisation: API Gateway caps a request body at 10 MB, which one real PDF exceeds.
    Nothing here triggers the ingest afterwards -- S3 emits an event on the new object and
    the index rebuilds on its own.
    """
    chosen = st.file_uploader("Upload a document", type=["pdf", "md", "markdown", "txt"],
                              label_visibility="collapsed")
    st.caption("PDF, Markdown or plain text. Other formats are refused rather than stored: a "
               "document that can never be indexed is worse than one never uploaded.")
    if chosen is None:
        return

    st.markdown(f"**{esc(chosen.name)}** · {human_size(chosen.size)}")
    if not st.button("Upload", type="primary", use_container_width=True):
        return

    status, grant = call_api(base_url, token, "/documents", payload={"filename": chosen.name})
    if status != 200:
        st.session_state.flash = ("upload", status, grant)
        st.rerun()
        return

    if chosen.size > grant.get("max_bytes", MAX_UPLOAD_HINT):
        # S3 would reject this too -- the size is a signed condition, not a client-side
        # courtesy. Catching it here only turns a 400 from S3 into a sentence.
        st.session_state.flash = ("upload", 413, {
            "message": f"Too large: {human_size(chosen.size)}, "
                       f"limit is {human_size(grant['max_bytes'])}."})
        st.rerun()
        return

    upload = grant["upload"]
    try:
        response = requests.post(
            upload["url"], data=upload["fields"],
            files={"file": (chosen.name, chosen.getvalue(),
                            upload["fields"].get("Content-Type", "application/octet-stream"))},
            timeout=UPLOAD_TIMEOUT)
        ok = response.status_code in (200, 201, 204)
        body = {"message": f"**{chosen.name}** uploaded. Indexing starts automatically and takes "
                           "about a minute." if ok else response.text[:300]}
        st.session_state.flash = ("upload", 200 if ok else response.status_code, body)
    except requests.RequestException as exc:
        st.session_state.flash = ("upload", 0, {"message": str(exc)})
    st.rerun()


def draw_stats(slot, health: dict) -> None:
    """The four headline numbers.

    Written into a placeholder because this panel renders at the top of the left column,
    before the question at the bottom of the script has been answered. Without the slot the
    question count is permanently one behind -- ask the first question, watch it say zero.
    Rewriting the slot afterwards costs nothing; `st.rerun()` would repeat the /health and
    /documents calls on every question.

    `kb_loaded` alone only says the artifact parsed. An index that loaded perfectly and holds
    nothing cannot answer a single question, and a green badge there sends someone debugging
    the model when the corpus is the problem.
    """
    loaded = health.get("kb_loaded") and health.get("chunk_count", 0) > 0
    slot.markdown(
        f'<div class="kb-panel" style="margin-top:.9rem;padding:.8rem 1.1rem">'
        f'<div class="kb-stat-row">'
        f'<div><div class="kb-stat-label">Status</div><div style="margin-top:.35rem">'
        f'{pill("Active", "ok") if loaded else pill("Empty", "warn")}</div></div>'
        f'<div><div class="kb-stat-label">Documents</div>'
        f'<div class="kb-stat-value">{health.get("document_count", 0)}</div></div>'
        f'<div><div class="kb-stat-label">Passages</div>'
        f'<div class="kb-stat-value">{health.get("chunk_count", 0)}</div></div>'
        f'<div><div class="kb-stat-label">Questions</div>'
        f'<div class="kb-stat-value">{len(st.session_state.history)}</div></div>'
        f'</div></div>', unsafe_allow_html=True)


def render_flash() -> None:
    """One place for the outcome of the last write, surviving the rerun that follows it."""
    flash = st.session_state.pop("flash", None)
    if not flash:
        return
    kind, status, body = flash
    if kind == "delete" and status == 202:
        note(f"Deleted <b>{esc(body.get('deleted'))}</b>. " + esc(body.get("message", "")), "info")
    elif kind == "upload" and status == 200:
        note(body.get("message", "Uploaded."), "info")
    else:
        note(f"{esc(body.get('message', 'Something went wrong.'))}", "danger")


# ---------------------------------------------------------------------- answer rendering


def as_plain_text(text: str) -> str:
    """Render a document excerpt verbatim.

    Excerpts are raw Markdown lifted out of the source documents, so Streamlit happily
    renders `## Service credits` as a heading and a pipe table as a table. That
    misrepresents what the retriever matched on, and it wrecks the layout.
    """
    for char in ("\\", "`", "*", "_", "#", "|", "[", "]", "<", ">"):
        text = text.replace(char, "\\" + char)
    return text


def render_answer(result: dict) -> None:
    st.markdown(result.get("answer", ""))

    grounding = result.get("grounding", "low")
    tone, label, meaning = GROUNDING.get(grounding, ("warn", grounding, ""))
    metadata = result.get("metadata", {})
    st.markdown(
        f'{pill(label, tone)}&nbsp;<span class="kb-src">{esc(meaning)}</span><br>'
        f'<span class="kb-src">Confidence <b>{result.get("confidence", 0):.2f}</b> · '
        f'{metadata.get("latency_ms", 0) / 1000:.1f} s · '
        f'{esc(metadata.get("style", "standard"))}</span>',
        unsafe_allow_html=True)

    render_sources(result.get("sources", []), abstained=grounding == "insufficient_context")
    render_debug(result)


def render_sources(sources: list[dict], abstained: bool = False) -> None:
    if not sources:
        st.caption("No passage scored above the relevance floor."
                   if abstained else "No sources.")
        return

    if abstained:
        # Retrieved and then rejected. Labelling them as evidence would be wrong, but hiding
        # them is worse: they usually show the right document was found and the wrong section
        # of it, which is a different problem from the document being absent.
        header = f"Retrieved but judged insufficient — {len(sources)}"
    else:
        cited = sum(1 for s in sources if s.get("cited"))
        header = f"Sources — {cited} of {len(sources)} cited"

    with st.expander(header):
        if abstained:
            st.caption("The model read these and reported they do not answer the question. "
                       "Shown so you can judge that yourself — not as support for the answer.")
        for source in sources:
            where = source.get("section") or (f"page {source['page']}" if source.get("page") else "")
            title = source.get("document_title") or source.get("document_id")
            mark = ("rejected" if abstained
                    else "cited" if source.get("cited") else "not cited")
            st.markdown(
                f'<div class="kb-src"><b>{esc(title)}</b>'
                f'{" › " + esc(where) if where else ""} · {source["score"]:.3f} · {esc(mark)}</div>',
                unsafe_allow_html=True)
            st.markdown(f'<div class="kb-quote">{esc(source.get("excerpt", ""))}</div>',
                        unsafe_allow_html=True)


def render_debug(result: dict) -> None:
    """Everything needed to chase a request into CloudWatch, in the UI.

    This is what makes the system debuggable from the client: a user reporting a bad answer
    can hand over one request id, and the query below reconstructs the run.
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
            f"+ 0.25·citation coverage `{components.get('citation_coverage')}`")
        if metadata.get("dropped_citations"):
            st.warning("Citations the model invented and the API removed: "
                       f"`{', '.join(metadata['dropped_citations'])}`")
        st.caption("Find this request in CloudWatch Logs Insights:")
        st.code("fields @timestamp, message, confidence, top_score, latency_ms\n"
                f'| filter correlation_id = "{request_id}"\n| sort @timestamp asc', language="text")
        st.json(result, expanded=False)


def render_error(status: int, body: dict) -> None:
    error = body.get("error", "unknown")
    st.error(f"**{error}** (HTTP {status}) — {body.get('message', '')}")
    if error in ERROR_HELP:
        st.info(ERROR_HELP[error])
    if body.get("hint"):
        st.info(body["hint"])
    if body.get("request_id"):
        st.caption(f"Request id: `{body['request_id']}`")


# ------------------------------------------------------------------------------- app


st.set_page_config(page_title="Knowledge Base", page_icon="\U0001F4DA", layout="wide",
                   initial_sidebar_state="expanded")

if "history" not in st.session_state:
    st.session_state.history = []
if "session_id" not in st.session_state:
    # One id per browser session, so a conversation can be reconstructed from the query log.
    st.session_state.session_id = str(uuid.uuid4())

# Read before the stylesheet is written, because it decides which palette gets written.
# Kept in the URL rather than session state so a browser reload does not discard the choice.
mode = st.query_params.get("theme", "System")
if mode not in theme.MODES:
    mode = "System"
theme.inject(mode)

base_url, token = read_config()

if not base_url or not token:
    # Only shown when there is nothing to connect with. The fallback exists so a live demo
    # can be recovered without editing a file.
    st.markdown('<div class="kb-title">Knowledge Base</div>'
                '<div class="kb-subtitle">Not configured yet.</div>', unsafe_allow_html=True)
    note("Run <code>python scripts/configure_client.py</code>, or enter the values below.", "warn")
    base_url = st.text_input("API base URL", value=base_url)
    token = st.text_input("API token", value=token, type="password")
    st.stop()

health_status, health = call_api(base_url, token, "/health")
list_status, listing = call_api(base_url, token, "/documents")
documents = listing.get("documents", []) if list_status == 200 else []
index = listing.get("index", {}) if list_status == 200 else {}

# -------------------------------------------------------------------- sidebar: the corpus

with st.sidebar:
    heading, action = st.columns([2, 1.15], vertical_alignment="center")
    heading.markdown('<div class="kb-title">Knowledge Base</div>'
                     '<div class="kb-subtitle">Manage your sources</div>', unsafe_allow_html=True)
    with action, st.popover("\uFF0B Add", use_container_width=True):
        render_upload(base_url, token)

    render_flash()

    stats_slot = st.empty()
    if health_status != 200:
        note(f"API unreachable \u2014 {esc(health.get('message', f'HTTP {health_status}'))}", "danger")
    else:
        draw_stats(stats_slot, health)

    # Drift, in two directions. The second is the dangerous one: content still being
    # answered from after its source was removed.
    pending, orphaned = index.get("pending_ingest", []), index.get("orphaned_in_index", [])
    if orphaned:
        note(f"Deleted but still searchable: <b>{esc(', '.join(orphaned))}</b>. Reindexing is "
             "running; answers may cite it for about a minute.", "warn")
    elif pending:
        note(f"Uploaded, not yet indexed: <b>{esc(', '.join(pending))}</b>. Indexing starts on "
             "its own and takes about a minute.", "info")
    elif index.get("kb_version") and health.get("kb_version") \
            and index["kb_version"] != health["kb_version"]:
        # The artifact is current and the answers are not. `in_sync` cannot see this state,
        # and it lasts up to a minute after every rebuild.
        note("Index rebuilt. The query path is still serving the previous one \u2014 a warm "
             "Lambda re-checks at most once a minute.", "info")

    render_recent(documents)
    render_list(base_url, token, documents)

    st.markdown("<hr>", unsafe_allow_html=True)
    chosen = st.segmented_control("Theme", theme.MODES, default=mode, key="theme_mode",
                                  label_visibility="collapsed")
    if chosen and chosen != mode:
        st.query_params["theme"] = chosen
        st.rerun()
    st.caption("System follows your operating system and keeps following it \u2014 the switch "
               "at dusk needs no reload.")

# ------------------------------------------------------------------------ main: the chat

st.markdown('<div class="kb-title">Assistant</div>'
            '<div class="kb-subtitle">Answers grounded in the documents beside you. Every claim '
            'carries a citation the API verifies against what was actually retrieved.</div>',
            unsafe_allow_html=True)

controls, _spacer = st.columns([2, 3])
with controls:
    settings, clear = st.columns(2)
    with settings.popover("Settings", use_container_width=True):
        top_k = st.slider("Passages to retrieve", 1, 10, 5)
        simple = st.toggle("Explain like I'm 10", value=False,
                           help="Plain language for someone new to the subject. Citations and "
                                "the refusal to answer beyond the documents are unchanged.")
        st.caption(f"Session `{st.session_state.session_id[:8]}`")
    if clear.button("Clear", use_container_width=True):
        st.session_state.history = []
        st.rerun()

history_area = st.container(height=470, border=False)
with history_area:
    # In a placeholder so the first question can clear it in the same run. Left as a plain
    # block it lingers above the answer it was meant to invite.
    empty_slot = st.empty()
    if not st.session_state.history:
        empty_slot.markdown('<div class="kb-empty">Ask a question about the documents.</div>',
                            unsafe_allow_html=True)
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
    empty_slot.empty()
    with history_area:
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"), st.spinner("Retrieving and generating\u2026"):
            status, body = call_api(base_url, token, "/query", {
                "question": question,
                "session_id": st.session_state.session_id,
                "top_k": top_k,
                "style": "simple" if simple else "standard",
            })
            if status == 200:
                render_answer(body)
            else:
                render_error(status, body)

    st.session_state.history.append(
        {"question": question, "status": status, "body": body, "error": status != 200})
    if health_status == 200:
        draw_stats(stats_slot, health)
