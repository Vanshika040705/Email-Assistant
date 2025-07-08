import streamlit as st
from backend import EmailReplySystem
import pandas as pd
import time
import matplotlib.pyplot as plt
import logging
from streamlit_autorefresh import st_autorefresh
import os
import pytz

# Enable auto-refresh every 15 seconds
st_autorefresh(interval=15 * 1000, key="refresh")

st.set_page_config(page_title="Automatic Email Reply System with Agentic AI", layout="wide")
st.title("📧 Automatic Email Reply System with Agentic AI")

# Initialize backend system (singleton)
if 'backend' not in st.session_state:
    st.session_state.backend = EmailReplySystem()
backend = st.session_state.backend

# Auto-refresh settings
REFRESH_INTERVAL = 15  # seconds
if 'last_refresh' not in st.session_state:
    st.session_state.last_refresh = time.time()

elapsed = time.time() - st.session_state.last_refresh
countdown = max(0, REFRESH_INTERVAL - int(elapsed))

st.markdown(
    f"""
    <style>
    .countdown-box {{
        position: fixed;
        top: 20px;
        right: 40px;
        background-color: #223344;
        color: #fff;
        padding: 8px 18px;
        border-radius: 10px;
        font-size: 1.1em;
        z-index: 9999;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    }}
    </style>
    <div class="countdown-box">
        🔄 Refreshing in <span id="countdown">{countdown}</span> seconds...
    </div>
    <script>
    let countdown = {countdown};
    let interval = setInterval(function() {{
        countdown -= 1;
        if (countdown < 0) {{
            clearInterval(interval);
        }} else {{
            document.getElementById("countdown").innerText = countdown;
        }}
    }}, 1000);
    </script>
    """,
    unsafe_allow_html=True
)

# Manual button for debugging
if st.button("Process Inbox Now (Manual Test)"):
    backend.process_inbox()
    st.success("Inbox processed!")
    st.session_state.last_refresh = time.time()
    st.rerun()

# Auto-refresh logic: only rerun when countdown hits zero
if elapsed >= REFRESH_INTERVAL:
    backend.process_inbox()
    st.session_state.last_refresh = time.time()
    st.rerun()

# --- Notification for new human review email ---
if 'last_notified_review_id' not in st.session_state:
    st.session_state.last_notified_review_id = None

# Tabs for navigation
review_emails = backend.get_human_review_emails()
if review_emails:
    st.warning(f"⚠️ {len(review_emails)} email(s) require your review in the 'Human Review' tab!")
    latest_review_id = review_emails[0]['id']
    if st.session_state.last_notified_review_id != latest_review_id:
        st.toast("A new email requires your review!", icon="✉️")
        st.session_state.last_notified_review_id = latest_review_id
tabs = st.tabs([
    "Dashboard",
    f"Human Review ({len(review_emails)})",
    "Statistics",
    "Threads",
    "Calendars",
    "Confirmed Meetings",
    "Settings"
])

# Dashboard Tab
with tabs[0]:
    st.header("Email Threads & Response History")
    if st.button("Clear Dashboard Data", type="secondary"):
        backend.clear_dashboard()
        st.success("Dashboard data cleared!")
        st.rerun()
    df = backend.get_dashboard_data()
    if not df.empty:
        # Show status column instead of subject
        show_df = df[["from", "status", "intent", "confidence", "sensitive", "date", "ai_reply"]]
        show_df.columns = [col.title() for col in show_df.columns]
        st.dataframe(show_df, use_container_width=False)
    else:
        st.info("No email data to display yet. The system auto-fetches every 15 seconds.")

# Human Review Tab
with tabs[1]:
    st.header("Human-in-the-Loop Review")
    if not review_emails:
        st.info("No emails need human review at the moment.")
    else:
        for item in review_emails:
            with st.expander(f"{item['subject']} (from {item['from']}) on {item['date']}"):
                st.markdown(f"**Sender:** {item['from']}")
                st.markdown(f"**Intent:** {item['intent']}")
                st.markdown(f"**Subject:** {item['subject']}")
                st.markdown(f"**Date:** {item['date']}")
                st.markdown(f"**Body:**\n{item['body']}")
                st.markdown(f"**AI Suggested Reply:**")
                edited_reply = st.text_area(
                    f"Edit reply for UID {item['uid']}",
                    value=item['ai_reply'],
                    key=f"reply_{item['uid']}"
                )
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Send Reply", key=f"send_{item['uid']}"):
                        backend.human_review_action(item['uid'], 'send', edited_reply)
                        st.success("Reply sent and removed from review queue.")
                        st.rerun()
                with col2:
                    if st.button("Skip Mail", key=f"skip_{item['uid']}"):
                        backend.human_review_action(item['uid'], 'skip')
                        st.info("Mail skipped and removed from review queue.")
                        st.rerun()

# Statistics Tab
with tabs[2]:
    st.header("Statistics")
    intent_counts, confidence_counts, status_counts = backend.get_statistics_data()
    def small_autopct(pct):
        return f"{pct:.1f}%" if pct > 0 else ""

    stat_cols = st.columns(3)
    fig_size = (5.5, 5.5)

    def render_legend(items, colors, title, ncol=1):
        legend_html = f'<div style="display:flex;flex-direction:column;align-items:center;margin-top:16px;margin-bottom:8px;">'
        legend_html += f'<div style="font-weight:bold;font-size:1.1em;margin-bottom:8px;">{title}</div>'
        legend_html += '<div style="display:flex;flex-direction:column;align-items:flex-start;">'
        for i, item in enumerate(items):
            color = '#%02x%02x%02x' % tuple(int(255*x) for x in colors[i][:3])
            legend_html += f'<div style="display:flex;align-items:center;margin-bottom:6px;">'
            legend_html += f'<span style="display:inline-block;width:22px;height:22px;background:{color};margin-right:12px;border-radius:4px;"></span>'
            legend_html += f'<span style="font-size:1.08em;">{item}</span>'
            legend_html += '</div>'
        legend_html += '</div></div>'
        st.markdown(legend_html, unsafe_allow_html=True)

    with stat_cols[0]:
        st.subheader("Intent Distribution")
        with st.container():
            if intent_counts:
                fig, ax = plt.subplots(figsize=fig_size)
                series = pd.Series(intent_counts)
                wedges, texts, autotexts = ax.pie(
                    series,
                    autopct=lambda pct: small_autopct(pct),
                    labels=None,
                    textprops={'fontsize': 12}
                )
                plt.tight_layout()
                st.pyplot(fig, clear_figure=True)
                # Render legend as markdown below (2 columns)
                colors = [w.get_facecolor() for w in wedges]
                render_legend(list(series.index), colors, "Intent", ncol=2)
            else:
                st.info("No data yet.")

    with stat_cols[1]:
        st.subheader("Confidence Levels")
        with st.container():
            if confidence_counts:
                fig, ax = plt.subplots(figsize=fig_size)
                series = pd.Series(confidence_counts)
                wedges, texts, autotexts = ax.pie(
                    series,
                    autopct=lambda pct: small_autopct(pct),
                    labels=None,
                    textprops={'fontsize': 12}
                )
                plt.tight_layout()
                st.pyplot(fig, clear_figure=True)
                # Render legend as markdown below (1 column)
                colors = [w.get_facecolor() for w in wedges]
                render_legend(list(series.index), colors, "Confidence", ncol=1)
            else:
                st.info("No data yet.")

    with stat_cols[2]:
        st.subheader("Status")
        with st.container():
            if status_counts:
                fig, ax = plt.subplots(figsize=fig_size)
                series = pd.Series(status_counts)
                wedges, texts, autotexts = ax.pie(
                    series,
                    autopct=lambda pct: small_autopct(pct),
                    labels=None,
                    textprops={'fontsize': 12}
                )
                plt.tight_layout()
                st.pyplot(fig, clear_figure=True)
                # Render legend as markdown below (1 column)
                colors = [w.get_facecolor() for w in wedges]
                render_legend(list(series.index), colors, "Status", ncol=1)
            else:
                st.info("No data yet.")

# Threads Tab
with tabs[3]:
    st.header("Email Threads")
    threads = backend.get_threads_data()
    if not threads:
        st.info("No threads to display yet.")
    else:
        thread_ids = list(threads.keys())
        # Build options with subject and sender for display
        def thread_label(thread_id):
            msgs = threads[thread_id]
            if msgs and isinstance(msgs, list):
                subject = msgs[0].get('subject', '(No Subject)')
                sender = msgs[0].get('from', '')
                return f"{subject} ({sender})"
            return f"Thread {thread_id}"
        selected_thread = st.selectbox(
            "Select a thread to view: ",
            thread_ids,
            format_func=thread_label
        )
        if selected_thread:
            st.subheader(f"Thread: {thread_label(selected_thread)}")
            for msg in threads[selected_thread]:
                st.markdown(f"**From:** {msg['from']}")
                st.markdown(f"**Subject:** {msg['subject']}")
                st.markdown(f"**Date:** {msg['date']}")
                st.markdown(f"**Body:** {msg['body']}")
                st.markdown(f"**AI Reply:** {msg['ai_reply']}")
                st.markdown(f"**Status:** {msg['status']}")
                st.markdown("---")

# Calendars Tab
with tabs[4]:
    st.header("Google Calendars & Events")
    
    # Prominent refresh section at the top
    st.markdown("---")
    st.markdown("### 🔄 Refresh Events")
    st.markdown("**When to refresh:** Click the button below to see the updated events from Google Calendar.")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("🔄 **Refresh Events Now**", type="primary", use_container_width=True):
            st.success("✅ Events refreshed! Check the table below for updates.")
            st.rerun()
    
    st.markdown("---")
    
    try:
        calendar_id = backend.calendar_id
        st.subheader(f"Calendar ID: {calendar_id}")
        events = backend.get_combined_events(calendar_id)
        # Add confirmed meetings as events
        confirmed = backend.get_confirmed_events()
        for conf in confirmed:
            slot = pd.to_datetime(conf.get('slot'), utc=True)  # Ensure tz-aware
            events.append({
                'title': conf.get('subject', 'Confirmed Meeting'),
                'start': slot,
                'end': slot + pd.Timedelta(hours=1),
                'location': '',
                'description': f"Confirmed via Email. Participants: {', '.join(conf.get('participants', []))}" if conf.get('participants') else 'Confirmed via Email.'
            })
        now = pd.Timestamp.now(tz="UTC")
        upcoming_events = []
        for event in events:
            start = pd.to_datetime(event['start'], utc=True)
            end = pd.to_datetime(event['end'], utc=True)
            if end > now:  # Only show future events
                upcoming_events.append(event)
        
        if upcoming_events:
            upcoming_events = sorted(upcoming_events, key=lambda x: pd.to_datetime(x['start']))
            df_events = pd.DataFrame(upcoming_events)
            # Ensure start/end columns are tz-aware (UTC)
            df_events['start'] = pd.to_datetime(df_events['start'], errors='coerce', utc=True)
            df_events['end'] = pd.to_datetime(df_events['end'], errors='coerce', utc=True)
            # Convert to local timezone (Asia/Kolkata)
            local_tz = pytz.timezone('Asia/Kolkata')
            def to_local(dt):
                try:
                    return dt.astimezone(local_tz) if pd.notnull(dt) else dt
                except Exception:
                    return dt
            df_events['start_local'] = df_events['start'].apply(to_local)
            df_events['end_local'] = df_events['end'].apply(to_local)
            # Sort by local start time
            df_events = df_events.sort_values('start_local')
            # Robustly format date and time, fallback to string if parsing fails
            def safe_dt_str(dt, fmt):
                try:
                    return dt.strftime(fmt) if pd.notnull(dt) else ''
                except Exception:
                    return str(dt) if dt else ''
            df_events['Start Date'] = df_events['start_local'].apply(lambda x: safe_dt_str(x, '%d-%m-%Y'))
            df_events['Start Time'] = df_events['start_local'].apply(lambda x: safe_dt_str(x, '%I:%M %p'))
            df_events['End Date'] = df_events['end_local'].apply(lambda x: safe_dt_str(x, '%d-%m-%Y'))
            df_events['End Time'] = df_events['end_local'].apply(lambda x: safe_dt_str(x, '%I:%M %p'))
            display_cols = ['title', 'Start Date', 'Start Time', 'End Date', 'End Time', 'location', 'description']
            df_events = df_events.reindex(columns=display_cols)
            df_events.columns = [col.title() for col in df_events.columns]
            st.dataframe(df_events, use_container_width=True)
    except Exception as e:
        st.error(f"Error fetching events: {e}")

# Confirmed Meetings Tab
with tabs[5]:
    st.header("Confirmed Meetings (via Email)")
    confirmed = backend.get_confirmed_events()
    if confirmed:
        df_confirmed = pd.DataFrame(confirmed)
        # Format slot as date/time string
        def safe_dt_str(dt, fmt):
            try:
                return pd.to_datetime(dt).strftime(fmt) if pd.notnull(dt) else ''
            except Exception:
                return str(dt) if dt else ''
        df_confirmed['Date'] = df_confirmed['slot'].apply(lambda x: safe_dt_str(x, '%d-%m-%Y'))
        df_confirmed['Time'] = df_confirmed['slot'].apply(lambda x: safe_dt_str(x, '%I:%M %p'))
        display_cols = ['subject', 'Date', 'Time', 'participants', 'confirmed_by', 'confirmed_at']
        df_confirmed = df_confirmed.reindex(columns=display_cols)
        df_confirmed.columns = [col.title().replace('_', ' ') for col in df_confirmed.columns]
        st.dataframe(df_confirmed, use_container_width=True)
    else:
        st.info("No confirmed meetings yet.")

# Settings Tab
with tabs[6]:
    st.header("Settings")
    st.subheader("AI Model")
    st.markdown("**Current AI Model: Gemini 1.5 Flash (Google Generative AI)**")
    st.markdown("---")
    if st.button("List Google Calendars (for debug)"):
        try:
            # Only show the configured calendar ID for debug
            calendar_id = backend.calendar_id
            st.write(f"Calendar ID: {calendar_id}")
        except Exception as e:
            st.error(f"Error: {e}") 