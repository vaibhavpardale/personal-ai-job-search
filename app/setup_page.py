"""Setup page: everything the automated pipeline needs, configured once.

Profile, résumé, LinkedIn import, the company job boards to watch, and the search settings.
"""

from contextlib import closing
import pandas as pd
import streamlit as st

import db
import discovery
from jobs_page import run_check_now
import linkedin_import
import profile_store
import settings

PROJECT_ROOT = db.PROJECT_ROOT


def _profile_section():
    st.subheader("Profile")
    st.caption("Used for matching and for tailoring your résumé. Only facts you enter here are used.")
    with closing(db.connect()) as conn:
        current = profile_store.get_profile(conn)
    with st.form("profile"):
        col1, col2 = st.columns(2)
        full_name = col1.text_input("Full name", value=current["full_name"])
        headline = col2.text_input("Headline", value=current["headline"])
        summary = st.text_area("Summary", value=current["summary"], height=100)
        col3, col4 = st.columns(2)
        years_experience = col3.text_input("Years of experience", value=current["years_experience"])
        education = col4.text_input("Education", value=current["education"])
        skills = st.text_area("Skills (comma-separated)", value=current["skills"])
        achievements = st.text_area(
            "Key achievements (one per line, with metrics where verified)",
            value=current["achievements"],
            height=120,
        )
        target_roles = st.text_area("Target roles (one per line)", value=current["target_roles"], height=80)
        industries = st.text_input("Preferred industries (comma-separated)", value=current["industries"])
        preferred_locations = st.text_input(
            "Preferred locations (comma-separated)", value=current["preferred_locations"]
        )
        if st.form_submit_button("Save profile"):
            with closing(db.connect()) as conn:
                profile_store.save_profile(
                    conn,
                    full_name=full_name,
                    headline=headline,
                    summary=summary,
                    years_experience=years_experience,
                    education=education,
                    skills=skills,
                    achievements=achievements,
                    target_roles=target_roles,
                    industries=industries,
                    preferred_locations=preferred_locations,
                )
            st.success("Profile saved.")


def _resume_section():
    st.subheader("Résumé")
    st.caption("Upload your current résumé. Each job's tailored PDF is written from it.")
    uploaded = st.file_uploader("Résumé file", type=["pdf", "docx", "txt", "md"], key="resume_upload")
    if uploaded is not None and st.button("Save résumé"):
        with closing(db.connect()) as conn:
            profile_store.save_document(conn, "Resume", uploaded.name, uploaded.getvalue())
        st.success(f"Saved {uploaded.name}")
        st.rerun()
    with closing(db.connect()) as conn:
        resume = profile_store.latest_resume(conn)
    if resume:
        st.write(f"Current résumé: **{resume['original_name']}** (uploaded {resume['uploaded_at']})")
    else:
        st.info("No résumé uploaded yet.")


def _linkedin_section():
    st.subheader("LinkedIn data")
    st.caption(
        "LinkedIn: Settings > Data privacy > Get a copy of your data. Include Profile, Positions, "
        "Education, and Skills, then upload the ZIP. Empty fields are filled, skills are merged, roles "
        "are added to your experience list, and anything you wrote is kept."
    )
    zip_file = st.file_uploader("LinkedIn export (ZIP)", type=["zip"], key="linkedin_zip")
    if zip_file is not None and st.button("Import LinkedIn export"):
        try:
            parsed = linkedin_import.parse_export(linkedin_import.read_export(zip_file.getvalue()))
        except ValueError as exc:
            st.error(str(exc))
        else:
            with closing(db.connect()) as conn:
                summary = linkedin_import.apply_export(conn, parsed)
            found = ", ".join(parsed["files_found"]) or "none of the expected files"
            st.success(
                f"Imported {summary['experience']} roles, {summary['education']} education entries, "
                f"and {summary['skills_added']} new skills. Files read: {found}."
            )
            st.rerun()
    with closing(db.connect()) as conn:
        roles = db.list_experience(conn)
    if roles:
        st.dataframe(
            pd.DataFrame(roles)[["title", "company", "started_on", "finished_on", "source"]],
            hide_index=True,
            width="stretch",
        )


def _watchlist_section():
    st.subheader("Company job boards to watch")
    st.caption(
        "Add the public job board of each company. Greenhouse boards are at boards.greenhouse.io/<name>, "
        "Lever boards at jobs.lever.co/<name>. The app reads only these public job lists."
    )
    with st.form("add_board", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        provider = col1.selectbox("Job board type", discovery.PROVIDERS)
        token = col2.text_input("Board name (from the URL) *")
        label = col3.text_input("Company name *")
        if st.form_submit_button("Add board"):
            if not token.strip() or not label.strip():
                st.error("Board name and company name are required.")
            else:
                with closing(db.connect()) as conn:
                    added = db.add_watched_board(conn, provider, token, label)
                if added:
                    st.success(f"Added {label.strip()}.")
                else:
                    st.warning("That board is already on the list.")

    with closing(db.connect()) as conn:
        boards = db.list_watched_boards(conn)
    if not boards:
        st.info("No boards yet. The automated check needs at least one.")
        return
    st.dataframe(pd.DataFrame(boards)[["label", "provider", "token"]], hide_index=True, width="stretch")
    choices = {board["id"]: f"{board['label']} ({board['provider']})" for board in boards}
    remove_id = st.selectbox("Remove a board", options=list(choices), format_func=choices.get)
    if st.button("Remove board"):
        with closing(db.connect()) as conn:
            db.remove_watched_board(conn, remove_id)
        st.rerun()


def _manual_run_section():
    st.subheader("Run the check")
    st.caption(
        "Runs the same check as the Jobs page: it reads your boards, scores new jobs, and prepares "
        "résumés for strong matches. It never submits anything."
    )
    if st.button("Run check"):
        run_check_now()
        st.caption("Results are listed on the Jobs and Review pages.")


def render():
    _profile_section()
    _resume_section()
    _linkedin_section()
    _watchlist_section()
    _manual_run_section()
