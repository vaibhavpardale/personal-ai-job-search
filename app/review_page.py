"""Review page: check each prepared application before anything is submitted by you.

For each job the pipeline prepared, you can download the PDF résumé, the cover letter, and the
review notes. Approve records your decision. Opening the posting and submitting it is always you.
"""

from contextlib import closing
from pathlib import Path

import streamlit as st

import db


def _download_buttons(item):
    folder = Path(item["output_dir"])
    if not folder.exists():
        st.warning("The prepared files for this job are missing. Run a check again to rebuild them.")
        return
    for path in sorted(folder.glob("*")):
        st.download_button(
            f"Download {path.name}",
            data=path.read_bytes(),
            file_name=path.name,
            key=f"dl_{item['id']}_{path.name}",
        )


def render():
    st.caption(
        "Each job below has a tailored PDF résumé and cover letter. Read the review notes, check the "
        "files, then approve or reject. Approving does not submit anything. You open the posting and apply yourself."
    )

    with closing(db.connect()) as conn:
        pending = db.list_queue(conn, "Pending")
        approved = db.list_queue(conn, "Approved")

    st.subheader(f"Waiting for your review ({len(pending)})")
    if not pending:
        st.info("Nothing is waiting for review. The next check will add strong matches here.")
    for item in pending:
        with st.container(border=True):
            st.markdown(
                f"**{item['title']}** at {item['company']} — fit {item['fit_score']}/100, {item['verdict']}"
                + (f" — {item['location']}" if item["location"] else "")
            )
            files_col, action_col = st.columns([3, 2])
            with files_col:
                _download_buttons(item)
            with action_col:
                if item["url"]:
                    st.link_button("Open posting", item["url"])
                approve_col, reject_col = st.columns(2)
                if approve_col.button("Approve", key=f"approve_{item['id']}"):
                    with closing(db.connect()) as conn:
                        db.set_queue_status(conn, item["id"], "Approved")
                    st.rerun()
                if reject_col.button("Reject", key=f"reject_{item['id']}"):
                    with closing(db.connect()) as conn:
                        db.set_queue_status(conn, item["id"], "Rejected")
                    st.rerun()

    if approved:
        st.subheader("Approved: submit these yourself")
        st.caption("After you submit an application, click Mark submitted so the follow-up reminder is set.")
        for item in approved:
            row_col, button_col = st.columns([4, 1])
            row_col.markdown(
                f"{item['title']} at {item['company']}"
                + (f" — [posting]({item['url']})" if item["url"] else "")
            )
            if button_col.button("Mark submitted", key=f"submitted_{item['id']}"):
                with closing(db.connect()) as conn:
                    db.update_status(conn, item["job_id"], "Submitted")
                st.rerun()
