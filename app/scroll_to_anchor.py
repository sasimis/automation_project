import streamlit as st
import streamlit.components.v1 as components

def scroll_to(anchor_id: str):
    components.html(
        f"""
        <script>
        const el = window.parent.document.getElementById("{anchor_id}");
        if (el) {{
            el.scrollIntoView({{behavior: "smooth", block: "center"}});
        }}
        </script>
        """,
        height=0,
        width=0,
    )