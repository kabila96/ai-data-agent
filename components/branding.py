"""Visual identity: theme CSS, profile card with the owner's photo, and page headers.

Design notes
------------
The look borrows from survey and topographic maps rather than generic dashboards:
an ink-blue legend panel (sidebar), cool map-paper background, a deep teal for
actions, and contour lines as the one decorative element, used only in page
headers. Typeface: Archivo throughout; headings use its wider cut.

Put your photo at ``assets/logo.jpg`` (or .png/.webp), or set BRAND_LOGO_PATH in
.env. It is cropped to a centred square automatically. Without a photo the app
shows your initials.
"""

from __future__ import annotations

import base64
import html
import io
from functools import lru_cache
from pathlib import Path

import streamlit as st

from config import PROJECT_ROOT, Settings

ASSETS = PROJECT_ROOT / "assets"
_LOGO_NAMES = ("logo.jpg", "logo.jpeg", "logo.png", "logo.webp")

INK = "#13293D"
INK_SOFT = "#2B4459"
PAPER = "#EEF2F0"
SURFACE = "#FFFFFF"
TEAL = "#0E7C7B"
TEAL_DARK = "#0A5E5D"
CONTOUR = "#8FA89B"
OCHRE = "#E0A526"
TEXT = "#13293D"
MUTED = "#566B7A"


# ------------------------------------------------------------------ logo
def logo_path(settings: Settings) -> Path | None:
    configured = getattr(settings, "brand_logo_path", None)
    candidates = [Path(configured)] if configured else []
    candidates += [ASSETS / n for n in _LOGO_NAMES]
    for path in candidates:
        path = path if path.is_absolute() else PROJECT_ROOT / path
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=4)
def _square_png(path_str: str, mtime: float, size: int = 320) -> bytes:
    """Centre-crop to a square and resize, so any portrait or landscape photo works."""
    from PIL import Image, ImageOps

    with Image.open(path_str) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        side = min(img.size)
        left, top = (img.width - side) // 2, max(0, (img.height - side) // 3)  # bias towards the face
        img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()


def logo_png(settings: Settings) -> bytes | None:
    path = logo_path(settings)
    if path is None:
        return None
    try:
        return _square_png(str(path), path.stat().st_mtime)
    except Exception:
        return None


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + parts[-1][0]) if len(parts) > 1 else (parts[0][:2] if parts else "PA")).upper()


def logo_data_uri(settings: Settings) -> str:
    png = logo_png(settings)
    if png:
        return "data:image/png;base64," + base64.b64encode(png).decode()
    letters = html.escape(initials(settings.brand_name))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">'
        f'<rect width="120" height="120" fill="{TEAL}"/>'
        f'<text x="60" y="74" text-anchor="middle" font-family="Archivo, Arial, sans-serif" '
        f'font-size="44" font-weight="700" fill="#fff">{letters}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def user_avatar(settings: Settings):  # type: ignore[no-untyped-def]
    """Avatar for the user's own chat messages: the photo if present."""
    png = logo_png(settings)
    return png if png else ":material/person:"


ASSISTANT_AVATAR = ":material/travel_explore:"


def page_icon(settings: Settings):  # type: ignore[no-untyped-def]
    """Favicon: the photo if present, otherwise a map emoji."""
    png = logo_png(settings)
    if png:
        from PIL import Image

        return Image.open(io.BytesIO(png))
    return "🗺️"


@lru_cache(maxsize=1)
def _contours_uri() -> str:
    svg_path = ASSETS / "contours.svg"
    if not svg_path.exists():
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(svg_path.read_bytes()).decode()


# ------------------------------------------------------------------ CSS
def _css() -> str:
    contours = _contours_uri()
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,300..800&display=swap');

:root {{
  --ink: {INK}; --ink-soft: {INK_SOFT}; --paper: {PAPER}; --surface: {SURFACE};
  --teal: {TEAL}; --teal-dark: {TEAL_DARK}; --contour: {CONTOUR}; --ochre: {OCHRE};
  --text: {TEXT}; --muted: {MUTED};
}}

html, body, [data-testid="stApp"], [data-testid="stApp"] button, [data-testid="stApp"] input,
[data-testid="stApp"] textarea, [data-testid="stApp"] select {{
  font-family: 'Archivo', 'Segoe UI', system-ui, sans-serif;
}}
[data-testid="stApp"] {{ background: var(--paper); color: var(--text); }}
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stMainBlockContainer"] {{ padding-top: 1.6rem; max-width: 1180px; }}

h1, h2, h3 {{ font-stretch: 112%; letter-spacing: -0.01em; color: var(--ink); }}
h2 {{ font-weight: 680; }}
h3 {{ font-weight: 640; }}
a {{ color: var(--teal); }}

/* ---------- Sidebar: the map legend ---------- */
[data-testid="stSidebar"] {{ background: var(--ink); }}
[data-testid="stSidebar"] * {{ color: #E4ECEA; }}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] {{ border-radius: 6px; padding: .3rem .55rem; }}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:hover {{ background: var(--ink-soft); }}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {{ color: #8FB3AD; font-size: .78rem;
  margin: .7rem 0 .1rem; }}
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {{
  background: var(--ink-soft); border-color: #3E5A70;
}}
[data-testid="stSidebar"] [data-baseweb="tag"] {{ background: var(--teal); }}

.pa-profile {{ display: flex; align-items: center; gap: .85rem; padding: .25rem 0 1.1rem;
  border-bottom: 1px solid #2E4A60; margin-bottom: .6rem; }}
.pa-profile img {{ width: 64px; height: 64px; border-radius: 50%; object-fit: cover;
  box-shadow: 0 0 0 3px var(--ink), 0 0 0 5px #5FC4BE; flex: none; }}
.pa-profile .pa-name {{ font-weight: 700; font-stretch: 110%; font-size: 1.02rem; line-height: 1.2; color: #fff; }}
.pa-profile .pa-tag {{ font-size: .8rem; color: #A9C2BD; line-height: 1.35; margin-top: .15rem; }}

.pa-legend {{ margin: .4rem 0 .2rem; font-size: .84rem; }}
.pa-legend .row {{ display: flex; align-items: center; gap: .55rem; padding: .22rem 0; color: #D3DFDC; }}
.pa-legend .dot {{ width: .62rem; height: .62rem; border-radius: 50%; flex: none; }}
.pa-legend .ok {{ background: #5FC4BE; }}
.pa-legend .warn {{ background: var(--ochre); }}
.pa-legend .off {{ background: #C9504A; }}

/* ---------- Page header with contour lines ---------- */
.pa-hero {{
  position: relative; overflow: hidden; border-radius: 14px; padding: 1.7rem 2rem 1.5rem;
  margin: 0 0 1.4rem; color: #fff;
  background: url("{contours}") center / cover no-repeat, linear-gradient(120deg, var(--ink) 0%, #164A55 100%);
}}
.pa-hero h1 {{ color: #fff; font-size: 2.05rem; font-weight: 720; font-stretch: 118%; margin: 0;
  padding: 0; line-height: 1.1; }}
.pa-hero p {{ color: #CFE3DF; margin: .45rem 0 0; max-width: 62ch; font-size: 1rem; }}
.pa-chips {{ display: flex; flex-wrap: wrap; gap: .45rem; margin-top: 1rem; }}
.pa-chip {{ font-size: .78rem; padding: .22rem .65rem; border-radius: 999px;
  background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.22); color: #EAF4F2; }}
.pa-chip.warn {{ background: rgba(224,165,38,.22); border-color: rgba(224,165,38,.55); }}

/* ---------- Content ---------- */
[data-testid="stExpander"] details {{ background: var(--surface); border: 1px solid #D5DED9; border-radius: 10px; }}
[data-testid="stChatMessage"] {{ background: var(--surface); border: 1px solid #DCE4E0; border-radius: 12px;
  padding: 1rem 1.1rem; }}
[data-testid="stChatMessageAvatarUser"] img, [data-testid="stChatMessage"] img[alt="user avatar"] {{ border-radius: 50%; }}
[data-testid="stChatInput"] {{ border-radius: 12px; border: 1px solid #C4D2CC; background: var(--surface); }}
[data-testid="stBottom"] > div {{ background: var(--paper); }}
[data-testid="stBaseButton-primary"] {{ background: var(--teal); border-color: var(--teal); }}
[data-testid="stBaseButton-primary"]:hover {{ background: var(--teal-dark); border-color: var(--teal-dark); }}
[data-testid="stMain"] [data-testid="stBaseButton-secondary"] {{ background: var(--surface); border-color: #C4D2CC; }}
[data-testid="stMain"] [data-testid="stBaseButton-secondary"]:hover {{ border-color: var(--teal); color: var(--teal); }}
[data-testid="stMetric"] {{ background: var(--surface); border: 1px solid #DCE4E0; border-radius: 10px; padding: .7rem .9rem; }}
:focus-visible {{ outline: 2px solid var(--ochre) !important; outline-offset: 2px; }}

@media (max-width: 640px) {{
  .pa-hero {{ padding: 1.2rem 1.1rem; border-radius: 10px; }}
  .pa-hero h1 {{ font-size: 1.55rem; }}
}}
</style>
"""


def apply_theme() -> None:
    st.html(_css())


# ------------------------------------------------------------------ pieces
def sidebar_profile(settings: Settings, status_rows: list[tuple[str, str]]) -> None:
    """Profile card with photo, then a legend-style status list: (state, text) with state ok|warn|off."""
    rows = "".join(
        f'<div class="row"><span class="dot {html.escape(state)}"></span>{html.escape(text)}</div>'
        for state, text in status_rows
    )
    st.sidebar.html(
        f"""
<div class="pa-profile">
  <img src="{logo_data_uri(settings)}" alt="{html.escape(settings.brand_name)}">
  <div><div class="pa-name">{html.escape(settings.brand_name)}</div>
       <div class="pa-tag">{html.escape(settings.brand_tagline)}</div></div>
</div>
<div class="pa-legend">{rows}</div>
"""
    )


def page_header(title: str, description: str = "", chips: list[tuple[str, str]] | None = None) -> None:
    """Contour-line header. chips: (text, kind) where kind is '' or 'warn'."""
    chip_html = "".join(
        f'<span class="pa-chip {html.escape(kind)}">{html.escape(text)}</span>' for text, kind in (chips or [])
    )
    st.html(
        f"""
<div class="pa-hero" role="banner">
  <h1>{html.escape(title)}</h1>
  {f'<p>{html.escape(description)}</p>' if description else ''}
  {f'<div class="pa-chips">{chip_html}</div>' if chip_html else ''}
</div>
"""
    )


def status_chips(services) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    chips: list[tuple[str, str]] = []
    if services.database_ready:
        chips.append((f"{services.settings.pg_database} (read-only)", ""))
        if services.security and services.security.postgis_version:
            chips.append((f"PostGIS {services.security.postgis_version}", ""))
    else:
        chips.append(("Database not connected", "warn"))
    chips.append((services.settings.openai_model if services.settings.openai_configured else "Chat model not set",
                  "" if services.settings.openai_configured else "warn"))
    return chips


# ------------------------------------------------------------------ access gate
def password_ok(settings: Settings, entered: str) -> bool:
    import hmac

    expected = settings.app_password.get_secret_value()
    return bool(expected) and hmac.compare_digest(entered.encode(), expected.encode())


def require_password(settings: Settings) -> None:
    """If APP_PASSWORD is set, ask for it once per browser session before showing anything."""
    if not settings.app_password.get_secret_value() or st.session_state.get("pa_authenticated"):
        return
    page_header(f"{settings.brand_name}", "This analytics workspace is private. Enter the access password to continue.")
    with st.form("pa_login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Open workspace", type="primary"):
            if password_ok(settings, entered):
                st.session_state.pa_authenticated = True
                st.rerun()
            st.error("That password is not correct.")
    st.stop()
