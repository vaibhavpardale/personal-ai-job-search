"""Jobs page: the ranked shortlist, the selected job, and reminders. Nothing here is manual work.

Layout follows the two-pane pattern of LinkedIn Jobs: a ranked list on the left and the selected
job's details on the right. The pipeline finds, scores, and prepares jobs; you review them on the
Review page.
"""

import json
from contextlib import closing
from datetime import date

import streamlit as st

import ai
import db
import discovery
import profile_store
import review_queue
import settings

SHORTLIST_LIMIT = 30


def _label(job):
    location = f" — {job['location']}" if job.get("location") else ""
    return f"{job['score']} · {job['title']} at {job['company']}{location}"


def run_check_now():
    """Run the same pipeline the scheduler runs, using the saved settings."""
    try:
        client = ai.get_client()
    except ai.MissingApiKeyError as exc:
        st.error(str(exc))
        return
    with closing(db.connect()) as conn:
        boards = db.list_watched_boards(conn)
        if not boards:
            st.info("Add company job boards on the Setup page first.")
            return
        saved = settings.load(conn)
        with st.spinner("Searching your boards, scoring new jobs, and preparing résumés for strong matches..."):
            summary = review_queue.run_daily(
                conn,
                boards,
                discovery.parse_keywords(saved["keywords"]),
                client,
                profile_store.get_profile(conn),
                profile_store.latest_resume_text(conn),
                locations=discovery.parse_keywords(saved["locations"]),
                exclude=discovery.parse_keywords(saved["exclude"]),
                min_score=saved["min_score"],
                max_analyses=saved["max_analyses"],
            )
    st.success(
        f"Found {summary['found']} matching postings, {summary['new']} new. "
        f"Scored {summary['analyzed']}; {summary['queued']} ready for review."
    )
    for error in summary["errors"]:
        st.warning(error)


def search_settings_section():
    st.caption("The check uses these. Titles must contain a keyword; excluded words drop a title.")
    with closing(db.connect()) as conn:
        current = settings.load(conn)
    with st.expander("Search settings", expanded=True), st.form("search_settings"):
        keywords = st.text_area("Title keywords (comma-separated)", value=current["keywords"], height=80)
        locations = st.text_area("Locations to keep (comma-separated, empty keeps all)", value=current["locations"], height=80)
        exclude = st.text_input("Drop titles containing (comma-separated)", value=current["exclude"])
        col1, col2 = st.columns(2)
        min_score = col1.number_input("Minimum fit score for review", min_value=0, max_value=100, value=current["min_score"])
        max_analyses = col2.number_input("Maximum AI analyses per run", min_value=1, max_value=50, value=current["max_analyses"])
        if st.form_submit_button("Save settings"):
            with closing(db.connect()) as conn:
                settings.save(
                    conn,
                    {
                        "keywords": keywords.strip(),
                        "locations": locations.strip(),
                        "exclude": exclude.strip(),
                        "min_score": int(min_score),
                        "max_analyses": int(max_analyses),
                    },
                )
            st.success("Settings saved.")


def render():
    search_settings_section()

    with closing(db.connect()) as conn:
        ranked = db.shortlist(conn)[:SHORTLIST_LIMIT]
        pending = db.list_queue(conn, "Pending")
        followups = db.list_open_followups(conn)
        submitted = db.list_jobs(conn, status="Submitted")

    cols = st.columns(4)
    cols[0].metric("Shortlisted", len(ranked))
    cols[1].metric("Ready for review", len(pending))
    cols[2].metric("Follow-ups due", len(followups))
    cols[3].metric("Submitted", len(submitted))

    if st.button("Run check now"):
        run_check_now()
        st.rerun()
    st.caption("The app also runs this check on a schedule. Each newly scored job uses one AI call.")

    if not ranked:
        st.info("No scored jobs yet. Add company boards and settings on the Setup page, then run a check.")
    else:
        list_col, detail_col = st.columns([2, 3])
        with list_col:
            st.markdown("**Shortlist**")
            selected_id = st.radio(
                "Jobs",
                options=[job["id"] for job in ranked],
                format_func={job["id"]: _label(job) for job in ranked}.get,
                label_visibility="collapsed",
                key="jobs_selected",
            )
        job = next(j for j in ranked if j["id"] == selected_id)
        analysis = json.loads(job["fit_analysis"])
        with detail_col:
            st.subheader(job["title"])
            st.write(f"**{job['company']}**" + (f" · {job['location']}" if job.get("location") else ""))
            st.metric("Fit", f"{analysis['score']} / 100", analysis["verdict"])
            st.write(analysis["summary"])
            strength_col, gap_col = st.columns(2)
            strength_col.markdown("**Why it fits**\n" + "\n".join(f"- {s}" for s in analysis["strengths"]))
            gap_col.markdown("**Gaps**\n" + "\n".join(f"- {g}" for g in analysis["gaps"]))
            if job.get("url"):
                st.link_button("View posting", job["url"])

    if followups:
        st.subheader("Follow-up reminders")
        today = date.today().isoformat()
        for item in followups:
            overdue = " (overdue)" if item["due_date"] and item["due_date"] < today else ""
            st.write(f"Due {item['due_date']}{overdue}: {item['title']} at {item['company']}")
