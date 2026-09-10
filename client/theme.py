"""The visual layer, kept apart from the app so the app stays readable.

Streamlit gives you components, not a design system. Everything below is the design system:
a set of tokens in two palettes, a handful of primitives built on them, and the minimum
number of Streamlit-internal selectors needed to get out of its default look.

Three rules held throughout:

*   **Internal selectors are quarantined.** Streamlit's DOM is not a public API and its class
    names change between releases. Everything that reaches into it lives in `_SHELL`, uses
    `data-testid` (the most stable handle Streamlit offers), and never styles anything by
    generated class name. The rest targets classes this file invents, on markup this file
    emits, which no upgrade can rename.

*   **No colour is a bare hex outside the palettes.** A palette scattered through a
    stylesheet is a palette nobody can change -- and with two of them, one that cannot be
    switched.

*   **Every Streamlit widget the app uses is re-coloured here.** Not for polish: Streamlit
    1.63 keeps its theme in JavaScript and exposes no CSS variables for it, so its own
    colours are baked into generated class names and cannot follow a runtime toggle. Any
    widget this file forgets stays light-on-light when the app goes dark. That is why the
    list below is long, and why it is a checklist rather than a flourish.
"""

from __future__ import annotations

import datetime as dt
import html

import streamlit as st

MODES = ("Light", "Dark", "System")

_LIGHT = """
  --page:        #F4F4F1;
  --surface:     #FFFFFF;
  --surface-alt: #FAFAF8;
  --surface-sunk:#EFEFEB;
  --line:        #E7E7E3;
  --line-soft:   #F0F0EC;

  --ink:         #191917;
  --ink-muted:   #6F6F67;
  --ink-faint:   #9B9B92;

  --accent:      #191917;
  --accent-hover:#000000;
  --accent-ink:  #FFFFFF;

  --ok:     #15803D;  --ok-bg:     #F0FDF4;  --ok-line:     #BBF7D0;
  --info:   #1D4ED8;  --info-bg:   #EFF6FF;  --info-line:   #BFDBFE;
  --warn:   #B45309;  --warn-bg:   #FFFBEB;  --warn-line:   #FDE68A;
  --danger: #B91C1C;  --danger-bg: #FEF2F2;  --danger-line: #FECACA;

  --shadow:      0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.04);
  --shadow-lift: 0 4px 12px rgba(16,24,40,.08), 0 2px 4px rgba(16,24,40,.04);
  --sheet-grad:  linear-gradient(160deg, #FCFCFB 0%, #F1F1ED 100%);
"""

# Not the light palette inverted. Inverting gives the classic too-dark, too-contrasty
# result: near-black surfaces with near-white text vibrate, and shadows vanish because
# nothing is darker than the page to cast onto. Surfaces here sit *above* the page instead,
# elevation is carried by a lighter border rather than a shadow, and the accent flips to
# near-white so a primary button stays the loudest thing on screen.
_DARK = """
  --page:        #131315;
  --surface:     #1B1B1E;
  --surface-alt: #212125;
  --surface-sunk:#0F0F11;
  --line:        #2E2E33;
  --line-soft:   #26262B;

  --ink:         #ECECEA;
  --ink-muted:   #A0A099;
  --ink-faint:   #74746E;

  --accent:      #ECECEA;
  --accent-hover:#FFFFFF;
  --accent-ink:  #131315;

  --ok:     #4ADE80;  --ok-bg:     #10241A;  --ok-line:     #1E5233;
  --info:   #7DA9FF;  --info-bg:   #14203A;  --info-line:   #274270;
  --warn:   #FBBF24;  --warn-bg:   #2A1F0A;  --warn-line:   #5C4310;
  --danger: #FB7185;  --danger-bg: #2C1216;  --danger-line: #6B2733;

  --shadow:      0 1px 2px rgba(0,0,0,.4), 0 1px 3px rgba(0,0,0,.3);
  --shadow-lift: 0 6px 18px rgba(0,0,0,.5), 0 2px 6px rgba(0,0,0,.35);
  --sheet-grad:  linear-gradient(160deg, #24242A 0%, #17171A 100%);
"""


def _palette(mode: str) -> str:
    """The token block for a mode.

    `System` is not a third palette. It is the light one plus a media query, so the browser
    decides and keeps deciding: the page follows the OS switching at dusk without a rerun,
    and this file never has to learn what the OS preference is.
    """
    if mode == "Dark":
        return f":root {{{_DARK}}}"
    if mode == "System":
        return (f":root {{{_LIGHT}}}\n"
                f"@media (prefers-color-scheme: dark) {{ :root {{{_DARK}}} }}")
    return f":root {{{_LIGHT}}}"


# ------------------------------------------------------- Streamlit's own DOM, quarantined

_SHELL = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="st-"], button, input, textarea, select {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI',
               'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', sans-serif !important;
  -webkit-font-smoothing: antialiased;
}
/* Restore the icon font the rule above clobbers. Streamlit draws its chevrons and close
   buttons as ligatures, so overriding font-family everywhere turns them into the literal
   word "expand_more" beside every popover. */
[data-testid="stIconMaterial"], span[data-testid*="Icon"], .material-symbols-rounded {
  font-family: 'Material Symbols Rounded' !important;
}

/* -- surfaces -------------------------------------------------------------------- */
[data-testid="stAppViewContainer"], [data-testid="stApp"] { background: var(--page); }
/* The chat input is fixed to the viewport bottom and Streamlit paints its own backdrop
   behind it, in its own theme. Left alone it draws a bar in the opposite palette across the
   width of the page -- the single most visible way its theme and this one can disagree. */
[data-testid="stBottom"], [data-testid="stBottom"] > div { background: var(--page) !important; }
[data-testid="stHeader"] { background: transparent; height: 0; }
[data-testid="stToolbar"] { right: 12px; }
[data-testid="stMainBlockContainer"] { padding: 2rem 2.2rem 1rem; max-width: 1060px; }
[data-testid="stVerticalBlockBorderWrapper"] { background: transparent; }
footer, #MainMenu { visibility: hidden; }

/* -- sidebar: the corpus lives here ---------------------------------------------- */
[data-testid="stSidebar"] {
  background: var(--surface); border-right: 1px solid var(--line);
  width: 400px !important; min-width: 400px !important;
}
[data-testid="stSidebarUserContent"] { padding: 1.5rem 1.15rem 2rem; }
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stSidebarCollapsedControl"] button { color: var(--ink-muted); }

/* -- text ------------------------------------------------------------------------ */
[data-testid="stAppViewContainer"], [data-testid="stSidebar"],
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li, [data-testid="stChatMessageContent"] { color: var(--ink); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
[data-testid="stWidgetLabel"] label, [data-testid="stWidgetLabel"] p { color: var(--ink-muted) !important; }
[data-testid="stMarkdownContainer"] a { color: var(--info); }
[data-testid="stMarkdownContainer"] code {
  background: var(--surface-alt); color: var(--ink-muted); border: 1px solid var(--line-soft);
  border-radius: 4px; padding: .05rem .28rem; font-size: .82em;
}
[data-testid="stMarkdownContainer"] strong { color: var(--ink); }
[data-testid="stCode"], [data-testid="stCode"] pre, [data-testid="stJson"] {
  background: var(--surface-sunk) !important; border: 1px solid var(--line);
  border-radius: var(--r-sm); color: var(--ink-muted);
}
[data-testid="stCode"] code span { color: var(--ink-muted) !important; }

/* -- buttons: the pill from the reference ---------------------------------------- */
.stButton > button, [data-testid="stPopover"] button {
  border-radius: 999px; border: 1px solid var(--line); background: var(--surface);
  color: var(--ink); font-weight: 500; font-size: .8rem; padding: .36rem .95rem;
  transition: background .12s ease, border-color .12s ease, transform .12s ease; box-shadow: none;
}
.stButton > button:hover, [data-testid="stPopover"] button:hover {
  background: var(--surface-alt); border-color: var(--ink-faint); color: var(--ink);
}
.stButton > button:active { transform: translateY(1px); }
.stButton > button[kind="primary"] {
  background: var(--accent) !important; color: var(--accent-ink) !important;
  border-color: var(--accent) !important;
}
.stButton > button[kind="primary"]:hover { background: var(--accent-hover) !important; }

/* The theme switch. Segmented rather than pill-shaped, so it reads as a set of states
   rather than three unrelated buttons. */
[data-testid="stSegmentedControl"] button {
  border-radius: var(--r-sm) !important; background: transparent !important;
  border: none !important; color: var(--ink-muted) !important; font-size: .74rem !important;
  padding: .22rem .6rem !important;
}
[data-testid="stSegmentedControl"] button[aria-checked="true"],
[data-testid="stSegmentedControl"] button[aria-pressed="true"] {
  background: var(--surface) !important; color: var(--ink) !important;
  /* Streamlit marks the selected segment with its own primary colour, which is the one
     colour in the app this file does not choose. */
  border: 1px solid var(--line) !important; box-shadow: var(--shadow);
}
[data-testid="stSegmentedControl"] > div {
  background: var(--surface-sunk); border: 1px solid var(--line);
  border-radius: var(--r-sm); padding: 2px;
}

/* -- inputs ----------------------------------------------------------------------- */
[data-testid="stChatInput"] {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--r-md); box-shadow: var(--shadow);
}
[data-testid="stChatInput"]:focus-within { border-color: var(--ink-faint); box-shadow: var(--shadow-lift); }
/* Streamlit paints the field itself one level in, in its own theme. Without this the
   input stays a light slab on a dark page. */
[data-testid="stChatInput"] > div { background: var(--surface) !important; }
[data-testid="stChatInput"] textarea { color: var(--ink) !important; background: transparent !important; }
[data-testid="stChatInput"] button { background: var(--surface-alt) !important; color: var(--ink-muted) !important; }
[data-testid="stChatInput"] textarea::placeholder { color: var(--ink-faint) !important; }
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
  background: var(--surface); color: var(--ink); border: 1px solid var(--line);
  border-radius: var(--r-sm);
}
[data-testid="stChatMessage"] { background: transparent; padding: .1rem 0; }
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] {
  background: var(--surface-alt); color: var(--ink-muted);
}

/* -- containers -------------------------------------------------------------------- */
[data-testid="stExpander"] details {
  border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface);
}
[data-testid="stExpander"] summary { font-size: .78rem; font-weight: 500; color: var(--ink-muted); }
[data-testid="stExpander"] summary:hover { color: var(--ink); }
[data-testid="stFileUploaderDropzone"] {
  background: var(--surface-alt); border: 1px dashed var(--line);
  border-radius: var(--r-md); color: var(--ink-muted);
}
[data-testid="stPopoverBody"] {
  border-radius: var(--r-md); border: 1px solid var(--line);
  background: var(--surface); box-shadow: var(--shadow-lift);
}
/* The chevron Streamlit appends to a popover trigger. Hidden rather than restyled: on a
   trigger whose whole label is one icon it reads as a second, meaningless glyph. Scoped to
   the last child so it does not also hide a Material icon used *as* the label. */
[data-testid="stPopoverButton"] > div > div:last-child:has([data-testid="stIconMaterial"]) {
  display: none;
}
[data-testid="stPopover"] button { justify-content: center; }
[data-testid="stPopover"] button p { margin: 0; }
[data-testid="stAlertContainer"] { border-radius: var(--r-md); border: 1px solid var(--line); }
hr { border-color: var(--line-soft); margin: .4rem 0; }

/* Scrollbars, so the transcript does not paint a light strip down a dark page. */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line); border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: var(--ink-faint); }

/* The row action, tinted red only on hover: a column of permanently red buttons is a
   warning nobody reads by the third row. */
[data-testid="stSidebar"] [data-testid="stPopover"] button:hover {
  background: var(--danger-bg); border-color: var(--danger-line); color: var(--danger);
}
"""

# ------------------------------------------------------------------ our own components

_COMPONENTS = """
:root { --r-sm: 8px; --r-md: 12px; --r-lg: 16px; }

.kb-title { font-size: 1.25rem; font-weight: 650; color: var(--ink); letter-spacing: -.02em; margin: 0; }
.kb-subtitle { font-size: .78rem; color: var(--ink-muted); margin: .18rem 0 0; }
.kb-section { font-size: .68rem; font-weight: 600; color: var(--ink-faint); letter-spacing: .06em;
              text-transform: uppercase; margin: 1.15rem 0 .5rem; }

/* -- document cards ---------------------------------------------------------------- */
.kb-cards { display: grid; grid-template-columns: repeat(2, 1fr); gap: .55rem; }
.kb-card { background: var(--surface-alt); border: 1px solid var(--line); border-radius: var(--r-md);
           overflow: hidden; transition: box-shadow .15s ease, transform .15s ease, border-color .15s ease; }
.kb-card:hover { box-shadow: var(--shadow-lift); transform: translateY(-2px); border-color: var(--ink-faint); }
.kb-card-preview { height: 68px; background: var(--sheet-grad); border-bottom: 1px solid var(--line-soft);
                   padding: .5rem .55rem; position: relative; overflow: hidden; }
.kb-card-sheet { background: var(--surface); border: 1px solid var(--line); border-radius: 4px;
                 padding: .35rem; height: 100%; box-shadow: var(--shadow); }
.kb-card-line { height: 3px; border-radius: 2px; background: var(--line); margin-bottom: 4px; }
.kb-card-line.w1 { width: 78%; } .kb-card-line.w2 { width: 92%; }
.kb-card-line.w3 { width: 60%; } .kb-card-line.w4 { width: 84%; }
.kb-card-badge { position: absolute; top: .35rem; right: .4rem; font-size: .54rem; font-weight: 600;
                 letter-spacing: .06em; color: var(--ink-faint); background: var(--surface);
                 border: 1px solid var(--line); border-radius: 4px; padding: .08rem .24rem; }
.kb-card-body { padding: .42rem .55rem .5rem; }
.kb-card-name { font-size: .7rem; font-weight: 550; color: var(--ink); line-height: 1.3;
                display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.kb-card-meta { font-size: .62rem; color: var(--ink-faint); margin-top: .2rem; }

/* -- list rows ---------------------------------------------------------------------- */
.kb-row-name { font-size: .77rem; color: var(--ink); font-weight: 500; line-height: 1.3;
               overflow-wrap: anywhere; }
.kb-row-meta { font-size: .66rem; color: var(--ink-faint); margin-top: .18rem; }

/* -- status pills -------------------------------------------------------------------- */
.kb-pill { display: inline-flex; align-items: center; gap: .3rem; font-size: .66rem; font-weight: 550;
           padding: .13rem .45rem .13rem .38rem; border-radius: 999px; border: 1px solid; white-space: nowrap; }
.kb-dot { width: 6px; height: 6px; border-radius: 50%; flex: 0 0 6px; }
.kb-pill.ok     { color: var(--ok);     background: var(--ok-bg);     border-color: var(--ok-line); }
.kb-pill.ok     .kb-dot { background: var(--ok); }
.kb-pill.info   { color: var(--info);   background: var(--info-bg);   border-color: var(--info-line); }
.kb-pill.info   .kb-dot { background: var(--info); }
.kb-pill.warn   { color: var(--warn);   background: var(--warn-bg);   border-color: var(--warn-line); }
.kb-pill.warn   .kb-dot { background: var(--warn); }
.kb-pill.danger { color: var(--danger); background: var(--danger-bg); border-color: var(--danger-line); }
.kb-pill.danger .kb-dot { background: var(--danger); }

/* -- panels ---------------------------------------------------------------------------- */
.kb-panel { background: var(--surface-alt); border: 1px solid var(--line);
            border-radius: var(--r-md); padding: .7rem .8rem; }
.kb-stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: .5rem; }
.kb-stat-label { font-size: .6rem; color: var(--ink-faint); text-transform: uppercase;
                 letter-spacing: .05em; white-space: nowrap; }
.kb-stat-value { font-size: 1.1rem; font-weight: 600; color: var(--ink); line-height: 1.25; }

.kb-note { font-size: .71rem; color: var(--ink-muted); background: var(--surface-alt);
           border: 1px solid var(--line); border-left: 3px solid var(--ink-faint);
           border-radius: var(--r-sm); padding: .45rem .6rem; margin: .4rem 0; }
.kb-note.warn   { background: var(--warn-bg);   border-color: var(--warn-line);
                  border-left-color: var(--warn); color: var(--warn); }
.kb-note.danger { background: var(--danger-bg); border-color: var(--danger-line);
                  border-left-color: var(--danger); color: var(--danger); }
.kb-note.info   { background: var(--info-bg);   border-color: var(--info-line);
                  border-left-color: var(--info); color: var(--info); }

.kb-empty { text-align: center; padding: 1.6rem 1rem; color: var(--ink-faint); font-size: .78rem;
            border: 1px dashed var(--line); border-radius: var(--r-md); background: var(--surface-alt); }
.kb-src { font-size: .72rem; color: var(--ink-muted); }
.kb-src b { color: var(--ink); font-weight: 550; }
.kb-quote { font-size: .72rem; color: var(--ink-muted); background: var(--surface-alt);
            border-left: 2px solid var(--line); padding: .35rem .6rem; margin: .25rem 0 .6rem;
            border-radius: 0 var(--r-sm) var(--r-sm) 0; }
"""


def inject(mode: str = "System") -> None:
    st.markdown(f"<style>{_palette(mode)}{_COMPONENTS}{_SHELL}</style>", unsafe_allow_html=True)


# ------------------------------------------------------------------------- primitives


def esc(text: object) -> str:
    """Everything interpolated into markup goes through here.

    Document names come from an S3 listing and excerpts come from the documents themselves.
    Neither is under this file's control, and both end up inside HTML.
    """
    return html.escape(str(text), quote=True)


def pill(label: str, tone: str = "ok") -> str:
    return f'<span class="kb-pill {tone}"><span class="kb-dot"></span>{esc(label)}</span>'


def note(text: str, tone: str = "") -> None:
    st.markdown(f'<div class="kb-note {tone}">{text}</div>', unsafe_allow_html=True)


def section(label: str) -> None:
    st.markdown(f'<div class="kb-section">{esc(label)}</div>', unsafe_allow_html=True)


def human_size(num_bytes: int) -> str:
    for unit, cutoff in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num_bytes >= cutoff:
            return f"{num_bytes / cutoff:.2f} {unit}"
    return f"{num_bytes} B"


def relative_time(iso: str) -> str:
    """"3 hours ago", the way the reference labels its cards."""
    if not iso:
        return "unknown"
    try:
        when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    seconds = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    if seconds < 90:
        return "just now"
    for cutoff, size, name in ((3600, 60, "min"), (86400, 3600, "hour"), (None, 86400, "day")):
        if cutoff is None or seconds < cutoff:
            value = int(seconds // size)
            return f"{value} {name}{'s' if value != 1 else ''} ago"
    return "a while ago"
