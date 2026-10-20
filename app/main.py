"""Streamlit entry point for the Personal AI Job Search Assistant.

Run from the project root with:
    streamlit run app/main.py
"""

import json
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

import ai
import discovery
import profile_store
import review_queue
from db import (
    STATUSES,
    add_contact,
    add_job,
    connect,
    get_contact,
    get_job,
    list_contacts,
    list_drafts,
    add_watched_board,
    import_discovered_job,
    list_queue,
    set_queue_status,
    list_jobs,
    list_prep_drafts,
    list_research,
    list_watched_boards,
    remove_watched_board,
    save_drafts,
    saved_external_keys,
    save_fit_analysis,
    save_prep_draft,
    save_research,
    update_status,
)

APP_TITLE = "Personal AI Job Search Assistant"

st.set_page_config(page_title=APP_TITLE, page_icon="🔎", layout="wide")

st.title(APP_TITLE)
st.caption("A local, human-in-the-loop tool for a better job search.")

with st.sidebar:
    st.header("Stages")
    st.markdown(
        "1. Project setup — done\n"
        "2. Minimal Streamlit shell — done\n"
        "3. SQLite schema and job tracker — done\n"
        "4. Profile and document storage — done\n"
        "5. OpenAI integration and job-fit analysis — done\n"
        "6. Résumé tailoring and cover letters — done\n"
        "7. Company and contact research — done\n"
        "8. Networking drafts and interview prep — done\n"
        "9. Job discovery from public job boards — done\n"
        "10. Review queue: daily search, match, and tailor — **this page**"
    )

tracker_tab, profile_tab, research_tab, outreach_tab, discover_tab, queue_tab = st.tabs(
    ["Tracker", "Profile", "Research", "Outreach & Prep", "Discover", "Review queue"]
)

with tracker_tab:
    st.subheader("Add a job")
    with st.form("add_job", clear_on_submit=True):
        col1, col2 = st.columns(2)
        title = col1.text_input("Job title *")
        company = col2.text_input("Company *")
        location = col1.text_input("Location")
        url = col2.text_input("Job URL")
        notes = st.text_area("Notes")
        description = st.text_area(
            "Job description (paste the posting text, needed for fit analysis)", height=180
        )
        submitted = st.form_submit_button("Save job")

    if submitted:
        if not title.strip() or not company.strip():
            st.error("Job title and company are required.")
        else:
            with closing(connect()) as conn:
                add_job(
                    conn,
                    title=title.strip(),
                    company=company.strip(),
                    location=location.strip() or None,
                    url=url.strip() or None,
                    notes=notes.strip() or None,
                    description=description.strip() or None,
                )
            st.success(f"Saved: {title.strip()} at {company.strip()}")

    st.subheader("Tracker")
    status_filter = st.selectbox("Show status", ["All", *STATUSES])

    with closing(connect()) as conn:
        jobs = list_jobs(conn, status=None if status_filter == "All" else status_filter)

    if not jobs:
        st.info("No jobs yet. Save one above to start tracking.")
    else:
        df = pd.DataFrame(jobs)[["id", "title", "company", "location", "status", "updated_at"]]
        st.dataframe(df, hide_index=True, width="stretch")

        st.markdown("**Update a job's status**")
        labels = {job["id"]: f"#{job['id']} — {job['title']} at {job['company']}" for job in jobs}
        col1, col2, col3 = st.columns([3, 2, 1])
        job_id = col1.selectbox("Job", options=list(labels), format_func=labels.get)
        new_status = col2.selectbox("New status", STATUSES)
        if col3.button("Update", width="stretch"):
            with closing(connect()) as conn:
                update_status(conn, job_id, new_status)
            st.rerun()

        st.markdown("**Job fit analysis**")
        fit_job_id = st.selectbox(
            "Job to analyze", options=list(labels), format_func=labels.get
        )
        if st.button("Analyze fit"):
            with closing(connect()) as conn:
                job = get_job(conn, fit_job_id)
                profile = profile_store.get_profile(conn)
                try:
                    analysis = ai.analyze_job_fit(ai.get_client(), profile, job)
                except ai.MissingApiKeyError as exc:
                    st.error(str(exc))
                except Exception as exc:  # network, quota, or malformed model output
                    st.error(f"Fit analysis failed: {exc}")
                else:
                    save_fit_analysis(conn, fit_job_id, analysis)
                    st.rerun()

        with closing(connect()) as conn:
            saved_job = get_job(conn, fit_job_id)
        if saved_job and saved_job["fit_analysis"]:
            analysis = json.loads(saved_job["fit_analysis"])
            st.metric("Fit score", f"{analysis['score']} / 100", analysis["verdict"])
            st.write(analysis["summary"])
            col1, col2 = st.columns(2)
            col1.markdown("**Strengths**\n" + "\n".join(f"- {s}" for s in analysis["strengths"]))
            col2.markdown("**Gaps**\n" + "\n".join(f"- {g}" for g in analysis["gaps"]))

        st.markdown("**Tailored résumé and cover letter**")
        st.caption(
            "Drafts use only your saved profile and the selected résumé. "
            "Review every line before you use it."
        )
        with closing(connect()) as conn:
            resume_files = [d for d in profile_store.list_documents(conn) if d["kind"] == "Resume"]
        resume_options = {None: "Profile only (no résumé)"}
        resume_options.update({d["id"]: d["original_name"] for d in resume_files})
        tailor_job_id = st.selectbox(
            "Job to tailor for", options=list(labels), format_func=labels.get, key="tailor_job"
        )
        resume_id = st.selectbox(
            "Source résumé", options=list(resume_options), format_func=resume_options.get
        )
        if st.button("Generate tailored draft"):
            with closing(connect()) as conn:
                job = get_job(conn, tailor_job_id)
                profile = profile_store.get_profile(conn)
                try:
                    resume_text = profile_store.document_text(conn, resume_id) if resume_id else ""
                    result = ai.tailor_application(ai.get_client(), profile, job, resume_text)
                except ai.MissingApiKeyError as exc:
                    st.error(str(exc))
                except Exception as exc:  # unreadable file, network, quota, or malformed output
                    st.error(f"Tailoring failed: {exc}")
                else:
                    save_drafts(
                        conn,
                        tailor_job_id,
                        {
                            "Tailored resume": ai.render_resume_draft(result),
                            "Cover letter": result["cover_letter"],
                        },
                    )
                    if result["unsupported_requirements"]:
                        st.warning(
                            "Not supported by your materials, so not claimed in the draft:\n"
                            + "\n".join(f"- {r}" for r in result["unsupported_requirements"])
                        )
                    if result["items_to_confirm"]:
                        st.info(
                            "Please confirm these before using the draft:\n"
                            + "\n".join(f"- {i}" for i in result["items_to_confirm"])
                        )
                    st.success("Draft saved below.")

        with closing(connect()) as conn:
            drafts = list_drafts(conn, tailor_job_id)
        if not drafts:
            st.caption("No drafts for this job yet.")
        for draft in drafts:
            with st.expander(f"{draft['kind']} — {draft['created_at']}"):
                st.code(draft["content"], language=None, wrap_lines=True)

with profile_tab:
    st.subheader("Your profile")
    st.caption(
        "Starting values come from your background. Fill in the rest and save. "
        "The AI uses only what is saved here."
    )

    with closing(connect()) as conn:
        current = profile_store.get_profile(conn)

    with st.form("profile"):
        full_name = st.text_input("Full name", value=current["full_name"])
        headline = st.text_input("Headline", value=current["headline"])
        summary = st.text_area("Summary", value=current["summary"], height=120)
        years_experience = st.text_input("Years of experience", value=current["years_experience"])
        education = st.text_input("Education", value=current["education"])
        skills = st.text_area("Skills (comma-separated)", value=current["skills"])
        achievements = st.text_area(
            "Key achievements (one per line, with metrics where verified)",
            value=current["achievements"],
            height=160,
        )
        profile_saved = st.form_submit_button("Save profile")

    if profile_saved:
        with closing(connect()) as conn:
            profile_store.save_profile(
                conn,
                full_name=full_name,
                headline=headline,
                summary=summary,
                years_experience=years_experience,
                education=education,
                skills=skills,
                achievements=achievements,
            )
        st.success("Profile saved.")

    st.subheader("Documents")
    st.caption("Uploaded files are stored in the local documents folder, which is git-ignored.")
    kind = st.selectbox("Document type", profile_store.DOCUMENT_KINDS)
    uploaded = st.file_uploader("Choose a file", type=["pdf", "docx", "txt", "md"])
    if uploaded is not None and st.button("Save document"):
        with closing(connect()) as conn:
            profile_store.save_document(conn, kind, uploaded.name, uploaded.getvalue())
        st.success(f"Saved {uploaded.name}")
        st.rerun()

    with closing(connect()) as conn:
        documents = profile_store.list_documents(conn)
    if documents:
        st.dataframe(
            pd.DataFrame(documents)[["kind", "original_name", "uploaded_at"]],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No documents yet.")

with research_tab:
    st.subheader("Company research")
    st.caption(
        "Searches the public web for company information and lists the sources it used. "
        "Check important facts against the sources before relying on them."
    )
    with closing(connect()) as conn:
        research_jobs = list_jobs(conn)

    if not research_jobs:
        st.info("Save a job in the Tracker first, then research its company here.")
    else:
        research_labels = {
            job["id"]: f"#{job['id']} — {job['title']} at {job['company']}" for job in research_jobs
        }
        research_job_id = st.selectbox(
            "Job", options=list(research_labels), format_func=research_labels.get, key="research_job"
        )
        research_job = next(job for job in research_jobs if job["id"] == research_job_id)

        if st.button("Research company"):
            try:
                result = ai.research_company(ai.get_client(), research_job["company"], research_job["title"])
            except ai.MissingApiKeyError as exc:
                st.error(str(exc))
            except Exception as exc:  # network, quota, or malformed model output
                st.error(f"Research failed: {exc}")
            else:
                with closing(connect()) as conn:
                    save_research(conn, research_job_id, result)
                st.rerun()

        with closing(connect()) as conn:
            history = list_research(conn, research_job_id)

        if not history:
            st.caption("No research for this company yet.")
        for index, entry in enumerate(history):
            research = entry["research"]
            heading = f"Research from {entry['created_at']}"
            with st.expander(heading, expanded=index == 0):
                st.write(research["summary"])
                if research["key_facts"]:
                    st.markdown("**Key facts**\n" + "\n".join(f"- {f}" for f in research["key_facts"]))
                if research["recent_news"]:
                    st.markdown("**Recent news**\n" + "\n".join(f"- {n}" for n in research["recent_news"]))
                if research["unknowns"]:
                    st.markdown("**Could not verify**\n" + "\n".join(f"- {u}" for u in research["unknowns"]))
                if research["sources"]:
                    st.markdown(
                        "**Sources**\n"
                        + "\n".join(f"- [{s['title']}]({s['url']})" for s in research["sources"])
                    )
                else:
                    st.caption("No web sources were returned for this research.")

    st.subheader("Contacts")
    st.caption(
        "Add people you find yourself, such as hiring managers or referrals. "
        "The app does not look up or scrape profiles."
    )
    with closing(connect()) as conn:
        all_jobs = list_jobs(conn)
    contact_jobs = {None: "No job linked"}
    contact_jobs.update({job["id"]: f"#{job['id']} — {job['title']}" for job in all_jobs})
    with st.form("add_contact", clear_on_submit=True):
        col1, col2 = st.columns(2)
        contact_name = col1.text_input("Name *")
        contact_role = col2.text_input("Role or title")
        contact_company = col1.text_input("Company")
        contact_url = col2.text_input("Profile URL")
        contact_job = st.selectbox("Linked job", options=list(contact_jobs), format_func=contact_jobs.get)
        contact_notes = st.text_area("Notes")
        contact_submitted = st.form_submit_button("Save contact")

    if contact_submitted:
        if not contact_name.strip():
            st.error("Name is required.")
        else:
            with closing(connect()) as conn:
                add_contact(
                    conn,
                    name=contact_name.strip(),
                    role=contact_role.strip() or None,
                    company=contact_company.strip() or None,
                    profile_url=contact_url.strip() or None,
                    notes=contact_notes.strip() or None,
                    job_id=contact_job,
                )
            st.success(f"Saved contact: {contact_name.strip()}")

    with closing(connect()) as conn:
        contacts = list_contacts(conn)
    if contacts:
        st.dataframe(
            pd.DataFrame(contacts)[["name", "role", "company", "profile_url", "created_at"]],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No contacts yet.")

with outreach_tab:
    st.subheader("Outreach and interview prep")
    st.caption(
        "Nothing here is sent. Drafts are for you to review, edit, and send yourself. "
        "Every draft uses only your saved profile, the job, and the contact details you entered."
    )
    with closing(connect()) as conn:
        outreach_jobs = list_jobs(conn)
        all_contacts = list_contacts(conn)

    if not outreach_jobs:
        st.info("Save a job in the Tracker first.")
    else:
        outreach_labels = {
            job["id"]: f"#{job['id']} — {job['title']} at {job['company']}" for job in outreach_jobs
        }
        outreach_job_id = st.selectbox(
            "Job", options=list(outreach_labels), format_func=outreach_labels.get, key="outreach_job"
        )
        outreach_job = next(job for job in outreach_jobs if job["id"] == outreach_job_id)

        st.markdown("**Networking and referral drafts**")
        contact_choices = {None: "Choose a contact"}
        contact_choices.update({c["id"]: f"{c['name']} ({c.get('role') or 'role not stated'})" for c in all_contacts})
        if not all_contacts:
            st.info("Add a contact in the Research tab first.")
        else:
            contact_id = st.selectbox("Contact", options=list(contact_choices), format_func=contact_choices.get)
            draft_kind = st.radio("Draft type", ai.OUTREACH_KINDS, horizontal=True)
            if st.button("Draft message", disabled=contact_id is None):
                with closing(connect()) as conn:
                    contact = get_contact(conn, contact_id)
                    profile = profile_store.get_profile(conn)
                    try:
                        result = ai.draft_outreach(ai.get_client(), profile, outreach_job, contact, draft_kind)
                    except ai.MissingApiKeyError as exc:
                        st.error(str(exc))
                    except Exception as exc:  # network, quota, or malformed model output
                        st.error(f"Drafting failed: {exc}")
                    else:
                        content = f"Subject: {result['subject']}\n\n{result['message']}\n"
                        save_prep_draft(conn, outreach_job["id"], draft_kind, content, contact_id=contact_id)
                        if result["items_to_confirm"]:
                            st.info(
                                "Please check before sending:\n"
                                + "\n".join(f"- {item}" for item in result["items_to_confirm"])
                            )
                        st.success("Draft saved below.")

        st.markdown("**Interview prep**")
        with closing(connect()) as conn:
            latest_resume = next(
                (d for d in profile_store.list_documents(conn) if d["kind"] == "Resume"), None
            )
        if latest_resume:
            st.caption(
                f"Questions come from the job description. Answers use only your profile and "
                f"the latest résumé ({latest_resume['original_name']})."
            )
        else:
            st.caption(
                "Questions come from the job description. Answers use only your profile, "
                "so upload a résumé under Profile for better answers."
            )
        if st.button("Prepare interview questions"):
            with closing(connect()) as conn:
                profile = profile_store.get_profile(conn)
                try:
                    resume_text = (
                        profile_store.document_text(conn, latest_resume["id"]) if latest_resume else ""
                    )
                    result = ai.interview_prep(ai.get_client(), profile, outreach_job, resume_text)
                except ai.MissingApiKeyError as exc:
                    st.error(str(exc))
                except Exception as exc:  # network, quota, or malformed model output
                    st.error(f"Interview prep failed: {exc}")
                else:
                    save_prep_draft(conn, outreach_job["id"], "Interview prep", ai.render_prep(result))
                    st.success("Interview prep saved below.")

        st.markdown("**Saved drafts for this job**")
        with closing(connect()) as conn:
            prep_drafts = list_prep_drafts(conn, outreach_job_id)
        if not prep_drafts:
            st.caption("No drafts for this job yet.")
        for draft in prep_drafts:
            with st.expander(f"{draft['kind']} — {draft['created_at']}"):
                st.code(draft["content"], language=None, wrap_lines=True)

with discover_tab:
    st.subheader("Watchlist")
    st.caption(
        "Add the public job board of each company you want to watch. Greenhouse boards are at "
        "boards.greenhouse.io/<name> and Lever boards at jobs.lever.co/<name>. The app reads only "
        "these public job lists."
    )
    with st.form("add_board", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        board_provider = col1.selectbox("Job board type", discovery.PROVIDERS)
        board_token = col2.text_input("Board name (from the board URL) *")
        board_label = col3.text_input("Company name to show *")
        board_submitted = st.form_submit_button("Add to watchlist")

    if board_submitted:
        if not board_token.strip() or not board_label.strip():
            st.error("Board name and company name are required.")
        else:
            with closing(connect()) as conn:
                added_id = add_watched_board(conn, board_provider, board_token, board_label)
            if added_id is None:
                st.warning("That board is already on the watchlist.")
            else:
                st.success(f"Added {board_label.strip()}.")

    with closing(connect()) as conn:
        boards = list_watched_boards(conn)

    if boards:
        st.dataframe(
            pd.DataFrame(boards)[["label", "provider", "token"]], hide_index=True, width="stretch"
        )
        board_choices = {board["id"]: f"{board['label']} ({board['provider']})" for board in boards}
        remove_id = st.selectbox("Remove a board", options=list(board_choices), format_func=board_choices.get)
        if st.button("Remove from watchlist"):
            with closing(connect()) as conn:
                remove_watched_board(conn, remove_id)
            st.rerun()
    else:
        st.info("Your watchlist is empty. Add a company board above to start.")

    st.subheader("Check for new jobs")
    keyword_text = st.text_input(
        "Keep titles containing any of these words (comma-separated, empty keeps all)",
        value="director of product, principal, head of product, vp product",
        help=(
            "Plain keyword matching. Broad words such as 'product' also match sales and "
            "support roles, so use senior title words."
        ),
    )
    if st.button("Check watchlist", disabled=not boards):
        with st.spinner("Reading the job boards..."):
            found, board_errors = discovery.discover(boards, discovery.parse_keywords(keyword_text))
        st.session_state["discovered"] = {"jobs": found, "errors": board_errors}

    results = st.session_state.get("discovered")
    if results:
        for error in results["errors"]:
            st.warning(error)
        if not results["jobs"]:
            st.info("No matching postings found on the watched boards.")
        else:
            with closing(connect()) as conn:
                already_saved = saved_external_keys(conn)
            new_jobs = {
                index: job
                for index, job in enumerate(results["jobs"])
                if (job["source"], job["external_id"]) not in already_saved
            }
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Saved": (job["source"], job["external_id"]) in already_saved,
                            "Title": job["title"],
                            "Company": job["company"],
                            "Location": job["location"] or "",
                            "Link": job["url"] or "",
                        }
                        for job in results["jobs"]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            picked = st.multiselect(
                "Save new postings to the tracker",
                options=list(new_jobs),
                format_func=lambda i: f"{new_jobs[i]['title']} — {new_jobs[i]['company']}",
            )
            if st.button("Save selected", disabled=not picked):
                with closing(connect()) as conn:
                    saved_count = sum(import_discovered_job(conn, new_jobs[i]) for i in picked)
                st.session_state["discover_message"] = f"Saved {saved_count} posting(s) to the Tracker."
                st.rerun()

    if "discover_message" in st.session_state:
        st.success(st.session_state.pop("discover_message"))

with queue_tab:
    st.subheader("Review queue")
    st.caption(
        "The app searches your watchlist, scores new postings against your profile and résumé, and "
        "prepares tailored files for strong matches. Nothing is submitted or sent. Approving only "
        "records your decision. You open the posting and apply yourself."
    )
    with closing(connect()) as conn:
        queue_boards = list_watched_boards(conn)

    if not queue_boards:
        st.info("Add company boards in the Discover tab first. The queue searches those boards.")
    else:
        col1, col2, col3 = st.columns([3, 1, 1])
        queue_keywords = col1.text_input(
            "Title keywords",
            value="director of product, principal, head of product, vp product",
            key="queue_keywords",
        )
        queue_min_score = col2.number_input(
            "Minimum fit score", min_value=0, max_value=100, value=review_queue.DEFAULT_MIN_SCORE
        )
        queue_max_analyses = col3.number_input(
            "Max AI analyses per run", min_value=1, max_value=50, value=review_queue.DEFAULT_MAX_ANALYSES
        )
        st.caption("Each analysis and each tailored draft is one paid AI call. The limit above caps them per run.")
        if st.button("Run daily check now"):
            try:
                client = ai.get_client()
            except ai.MissingApiKeyError as exc:
                st.error(str(exc))
            else:
                with st.spinner("Searching, scoring, and preparing drafts..."):
                    with closing(connect()) as conn:
                        summary = review_queue.run_daily(
                            conn,
                            queue_boards,
                            discovery.parse_keywords(queue_keywords),
                            client,
                            profile_store.get_profile(conn),
                            profile_store.latest_resume_text(conn),
                            min_score=int(queue_min_score),
                            max_analyses=int(queue_max_analyses),
                        )
                st.success(
                    f"Found {summary['found']} postings, {summary['new']} new. "
                    f"Analyzed {summary['analyzed']}, added {summary['queued']} to the queue."
                )
                for error in summary["errors"]:
                    st.warning(error)

        st.markdown("**Waiting for your review**")
        with closing(connect()) as conn:
            pending = list_queue(conn, "Pending")
        if not pending:
            st.info("Nothing is waiting for review.")
        for item in pending:
            with st.container(border=True):
                st.markdown(
                    f"**{item['title']}** at {item['company']} — "
                    f"fit {item['fit_score']}/100, {item['verdict']}"
                    + (f" — {item['location']}" if item["location"] else "")
                )
                analysis = json.loads(item["fit_analysis"])
                st.write(analysis["summary"])
                gap_col, file_col = st.columns(2)
                gap_col.markdown("**Gaps**\n" + "\n".join(f"- {g}" for g in analysis["gaps"]))
                folder = Path(item["output_dir"])
                with file_col:
                    for path in sorted(folder.glob("*")):
                        st.download_button(
                            f"Download {path.name}",
                            data=path.read_bytes(),
                            file_name=path.name,
                            key=f"dl_{item['id']}_{path.name}",
                        )
                action_col1, action_col2, action_col3 = st.columns(3)
                if item["url"]:
                    action_col1.link_button("Open posting", item["url"])
                if action_col2.button("Approve", key=f"approve_{item['id']}"):
                    with closing(connect()) as conn:
                        set_queue_status(conn, item["id"], "Approved")
                    st.rerun()
                if action_col3.button("Reject", key=f"reject_{item['id']}"):
                    with closing(connect()) as conn:
                        set_queue_status(conn, item["id"], "Rejected")
                    st.rerun()

        with closing(connect()) as conn:
            approved = list_queue(conn, "Approved")
        if approved:
            st.markdown("**Approved: you still submit these yourself**")
            st.caption("After you submit, mark the job Applied in the Tracker.")
            for item in approved:
                st.markdown(f"- {item['title']} at {item['company']}" + (f" — [posting]({item['url']})" if item["url"] else ""))
