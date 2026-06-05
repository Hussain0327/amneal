"""Amneal brand styling for the Streamlit UI.

Echoes the Amneal logo: a golden-yellow flowing brush-script wordmark on a
clean, professional surface. We render the wordmark with a brush-script web
font (Yellowtail) tinted in the brand gold rather than shipping an image, so it
stays crisp at any size and recolours with the theme.
"""

from __future__ import annotations

import streamlit as st

# Brand palette -------------------------------------------------------------
GOLD = "#F5B400"  # primary Amneal gold
GOLD_DEEP = "#D99400"  # darker gold for gradient depth / hovers
GOLD_SOFT = "#FFF8E6"  # warm sand tint for surfaces
INK = "#16213A"  # deep navy ink for text
INK_SOFT = "#5A6478"  # muted ink for captions

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Yellowtail&family=Inter:wght@400;500;600;700&display=swap');

:root {{
    --amneal-gold: {GOLD};
    --amneal-gold-deep: {GOLD_DEEP};
    --amneal-ink: {INK};
}}

/* Base type */
html, body, [class*="css"] {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

/* The Amneal wordmark — flowing brush script in brand gold */
.amneal-wordmark {{
    font-family: 'Yellowtail', cursive;
    background: linear-gradient(95deg, {GOLD} 0%, {GOLD_DEEP} 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    color: {GOLD};
    line-height: 1;
    display: inline-block;
    letter-spacing: 0.5px;
    filter: drop-shadow(0 1px 1px rgba(217, 148, 0, 0.18));
}}
.amneal-wordmark.lg {{ font-size: 4.2rem; }}
.amneal-wordmark.sm {{ font-size: 2.4rem; }}

.amneal-header {{
    display: flex;
    align-items: baseline;
    gap: 0.9rem;
    padding: 0.2rem 0 0.4rem 0;
    border-bottom: 3px solid {GOLD};
    margin-bottom: 1.4rem;
}}
.amneal-product {{
    font-weight: 700;
    font-size: 1.05rem;
    letter-spacing: 0.32em;
    color: {INK};
    text-transform: uppercase;
}}
.amneal-tagline {{
    color: {INK_SOFT};
    font-size: 0.9rem;
    margin: -0.6rem 0 1.2rem 0;
}}

/* Sidebar — sand surface with a gold edge */
section[data-testid="stSidebar"] {{
    background: {GOLD_SOFT};
    border-right: 1px solid {GOLD};
}}
section[data-testid="stSidebar"] .amneal-wordmark.sm {{ margin-bottom: 0.1rem; }}

/* Headings */
h1, h2, h3 {{ color: {INK}; font-weight: 700; }}

/* Buttons — solid gold, dark ink label */
.stButton > button, .stFormSubmitButton > button {{
    background: linear-gradient(95deg, {GOLD} 0%, {GOLD_DEEP} 100%);
    color: {INK};
    font-weight: 600;
    border: none;
    border-radius: 0.6rem;
    transition: filter 0.15s ease, transform 0.05s ease;
}}
.stButton > button:hover, .stFormSubmitButton > button:hover {{
    filter: brightness(1.05);
    color: {INK};
}}
.stButton > button:active, .stFormSubmitButton > button:active {{
    transform: translateY(1px);
}}

/* Inputs — gold focus ring */
.stTextInput input:focus, .stTextArea textarea:focus {{
    border-color: {GOLD} !important;
    box-shadow: 0 0 0 2px rgba(245, 180, 0, 0.25) !important;
}}

/* Source / alert cards */
.amneal-card {{
    background: #FFFFFF;
    border: 1px solid {GOLD_SOFT};
    border-left: 4px solid {GOLD};
    border-radius: 0.6rem;
    padding: 0.85rem 1.1rem;
    margin-bottom: 0.75rem;
    box-shadow: 0 1px 3px rgba(22, 33, 58, 0.05);
}}
</style>
"""


def inject_css() -> None:
    """Inject the Amneal brand stylesheet. Call once near the top of the app."""
    st.markdown(_CSS, unsafe_allow_html=True)


def render_header(product: str = "REGWATCH", tagline: str | None = None) -> None:
    """Render the main-area Amneal wordmark with the product name."""
    st.markdown(
        f'<div class="amneal-header">'
        f'<span class="amneal-wordmark lg">Amneal</span>'
        f'<span class="amneal-product">{product}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if tagline:
        st.markdown(f'<div class="amneal-tagline">{tagline}</div>', unsafe_allow_html=True)


def render_sidebar_wordmark() -> None:
    """Render the smaller Amneal wordmark in the sidebar."""
    st.sidebar.markdown(
        '<span class="amneal-wordmark sm">Amneal</span>',
        unsafe_allow_html=True,
    )
