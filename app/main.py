"""Streamlit entry point for the Personal AI Job Search Assistant.

Three pages: Jobs (what the pipeline found), Review (approve each prepared application), and
Setup (profile, résumé, LinkedIn data, job boards, and search settings).

Run from the project root with:
    streamlit run app/main.py
"""

import streamlit as st

import jobs_page
import review_page
import setup_page

APP_TITLE = "Personal AI Job Search Assistant"

st.set_page_config(page_title=APP_TITLE, page_icon="🔎", layout="wide")

st.title(APP_TITLE)
st.caption("Finds and prepares applications automatically. You review and submit each one yourself.")

jobs_tab, review_tab, setup_tab = st.tabs(["Jobs", "Review", "Setup"])

with jobs_tab:
    jobs_page.render()

with review_tab:
    review_page.render()

with setup_tab:
    setup_page.render()
