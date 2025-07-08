import os
import imapclient
import smtplib
import email
from email.message import EmailMessage
from email.header import decode_header
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import pandas as pd
import pickle
import base64
import re
import google.generativeai as genai
import logging
import json
import time
import traceback
import streamlit as st
import pytz

logging.basicConfig(level=logging.INFO)

class EmailReplySystem:
    def __init__(self):
        load_dotenv()
        self.email_address = os.getenv("EMAIL_ADDRESS")
        self.app_password = os.getenv("EMAIL_APP_PASSWORD")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.human_review_queue = []  # List of dicts for emails needing review
        self.sent_history = []        # List of dicts for sent replies
        self.dashboard_data = []      # List of dicts for dashboard
        self.threads = {}             # Dict of thread_id: [messages]
        self.pending_meetings = {}  # thread_id: {'last_suggested': ..., 'subject': ..., 'participants': ...}
        self.confirmed_events = []  # List of dicts for confirmed meetings
        self.init_gmail()
        self.init_gemini()
        self.ensure_needs_review_label()
        self.load_dashboard()
        self.init_calendar()

    def init_gmail(self):
        self.imap = imapclient.IMAPClient('imap.gmail.com', ssl=True)
        self.imap.login(self.email_address, self.app_password)

    def ensure_needs_review_label(self):
        # Create 'Needs Review' label if it doesn't exist
        try:
            self.imap.select_folder('[Gmail]/All Mail')
            labels = self.imap.list_folders()
            if not any('Needs Review' in l[2] for l in labels):
                self.imap.create_folder('Needs Review')
        except Exception as e:
            logging.warning(f"Could not ensure 'Needs Review' label: {e}")

    def move_to_needs_review_label(self, uid):
        try:
            with imapclient.IMAPClient('imap.gmail.com', ssl=True) as imap:
                imap.login(self.email_address, self.app_password)
                # Try to select the folder where the email is, fallback to INBOX
                try:
                    imap.select_folder('INBOX')
                except Exception:
                    imap.select_folder('[Gmail]/All Mail')
                imap.copy([uid], 'Needs Review')
                imap.add_flags([uid], [imapclient.SEEN])
        except Exception as e:
            logging.warning(f"Could not move email UID {uid} to 'Needs Review': {e}")

    def init_gemini(self):
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_model = genai.GenerativeModel('models/gemini-1.5-flash')

    def init_calendar(self):
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        self.creds = service_account.Credentials.from_service_account_file(
            'credentials.json', scopes=SCOPES)
        self.calendar_service = build('calendar', 'v3', credentials=self.creds)
        # Use environment variable for calendar ID, fallback to default
        self.calendar_id = os.environ.get('GOOGLE_CALENDAR_ID', 'emailassistant25@gmail.com')

    def check_calendar(self, meeting_time):
        try:
            dt = pd.to_datetime(meeting_time)
            # If dt is naive, localize to calendar's timezone (Asia/Kolkata)
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                local_tz = pytz.timezone('Asia/Kolkata')
                dt = local_tz.localize(dt)
            requested_start = dt
            requested_end = dt + pd.Timedelta(hours=1)
            time_min = (requested_start - pd.Timedelta(hours=2)).isoformat()
            time_max = (requested_end + pd.Timedelta(hours=2)).isoformat()
            # Google Calendar API expects UTC with Z
            time_min_utc = pd.to_datetime(time_min).astimezone(pytz.UTC).isoformat().replace('+00:00', 'Z')
            time_max_utc = pd.to_datetime(time_max).astimezone(pytz.UTC).isoformat().replace('+00:00', 'Z')
        except Exception:
            print(f"[DEBUG] Could not parse meeting_time: {meeting_time}")
            return 'unknown', []
        print(f"[DEBUG] Requested meeting time: {meeting_time} (tz: {dt.tzinfo})")
        print(f"[DEBUG] Checking calendar from {time_min_utc} to {time_max_utc}")
        events_result = self.calendar_service.events().list(
            calendarId=self.calendar_id,
            timeMin=time_min_utc,
            timeMax=time_max_utc,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        for event in events:
            start = pd.to_datetime(event['start'].get('dateTime'))
            end = pd.to_datetime(event['end'].get('dateTime'))
            print(f"[DEBUG] Found event: {start} to {end} (tz: {start.tzinfo} to {end.tzinfo})")
            # Convert both to UTC for comparison
            start_utc = start.tz_convert('UTC') if hasattr(start, 'tz_convert') and start.tzinfo else start.tz_localize('UTC') if start.tzinfo is None else start.astimezone(pytz.UTC)
            end_utc = end.tz_convert('UTC') if hasattr(end, 'tz_convert') and end.tzinfo else end.tz_localize('UTC') if end.tzinfo is None else end.astimezone(pytz.UTC)
            req_start_utc = requested_start.tz_convert('UTC') if hasattr(requested_start, 'tz_convert') and requested_start.tzinfo else requested_start.tz_localize('UTC') if requested_start.tzinfo is None else requested_start.astimezone(pytz.UTC)
            req_end_utc = requested_end.tz_convert('UTC') if hasattr(requested_end, 'tz_convert') and requested_end.tzinfo else requested_end.tz_localize('UTC') if requested_end.tzinfo is None else requested_end.astimezone(pytz.UTC)
            print(f"[DEBUG] Comparing: req_start_utc={req_start_utc}, req_end_utc={req_end_utc}, event_start_utc={start_utc}, event_end_utc={end_utc}")
            if start_utc < req_end_utc and end_utc > req_start_utc:
                print(f"[DEBUG] Conflict detected with event: {start} to {end}")
                return 'busy', self.suggest_alternative_slots(dt)
        print(f"[DEBUG] No conflict found. Slot is free.")
        return 'free', []

    def suggest_alternative_slots(self, dt):
        alt_slots = []
        for i in range(1, 6):
            new_start = dt + pd.Timedelta(hours=i)
            new_end = new_start + pd.Timedelta(hours=1)
            # Ensure timezone-aware and convert to UTC for Google Calendar API
            if new_start.tzinfo is None or new_start.tzinfo.utcoffset(new_start) is None:
                local_tz = pytz.timezone('Asia/Kolkata')
                new_start = local_tz.localize(new_start)
                new_end = local_tz.localize(new_end)
            new_start_utc = new_start.astimezone(pytz.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
            new_end_utc = new_end.astimezone(pytz.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
            events_result = self.calendar_service.events().list(
                calendarId=self.calendar_id,
                timeMin=new_start_utc,
                timeMax=new_end_utc,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            if not events:
                alt_slots.append(new_start.strftime('%Y-%m-%d %H:%M'))
            if len(alt_slots) >= 3:
                break
        return alt_slots

    def list_all_calendars(self):
        calendars_result = self.calendar_service.calendarList().list().execute()
        calendars = calendars_result.get('items', [])
        return calendars

    def list_events_for_calendar(self, calendar_id, max_results=10):
        events_result = self.calendar_service.events().list(
            calendarId=calendar_id,
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        return events

    def fetch_emails(self):
        # Always create a new IMAP connection for reliability
        with imapclient.IMAPClient('imap.gmail.com', ssl=True) as imap:
            imap.login(self.email_address, self.app_password)
            imap.select_folder('INBOX', readonly=False)
            messages = imap.search(['UNSEEN'])
            emails = []
            for uid in messages:
                raw_message = imap.fetch([uid], ['RFC822'])[uid][b'RFC822']
                msg = email.message_from_bytes(raw_message)
                subject, encoding = decode_header(msg['Subject'])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or 'utf-8', errors='ignore')
                from_addr = email.utils.parseaddr(msg.get('From'))[1]
                date = msg.get('Date')
                body = self.get_body(msg)
                thread_id = msg.get('In-Reply-To', msg.get('Message-ID', str(uid)))
                emails.append({
                    'uid': uid,
                    'from': from_addr,
                    'subject': subject,
                    'date': date,
                    'body': body,
                    'msg_obj': msg,
                    'thread_id': thread_id
                })
            logging.info(f"Fetched {len(emails)} emails from inbox.")
            return emails

    def get_body(self, msg):
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdispo = str(part.get('Content-Disposition'))
                if ctype == 'text/plain' and 'attachment' not in cdispo:
                    return part.get_payload(decode=True).decode('utf-8', errors='ignore')
        else:
            return msg.get_payload(decode=True).decode('utf-8', errors='ignore')
        return ""

    def extract_sender_name(self, email_obj):
        msg_obj = email_obj.get('msg_obj')
        if msg_obj:
            name, addr = email.utils.parseaddr(msg_obj.get('From'))
            if name:
                return name.split()[0]
        return email_obj['from'].split('@')[0].capitalize()

    def analyze_email(self, email_obj):
        prompt = f'''
You are an AI assistant for a professor. Classify the following email as one of:
- "meeting_request"
- "confirmation"
- "other"
Respond ONLY in this JSON format:
{{
  "intent": "meeting_request" or "confirmation" or "other",
  "sensitive": <true/false>,
  "meeting_time": "<meeting time or empty string, in ISO 8601 format if possible, otherwise best guess>",
  "confidence": "high/medium/low"
}}

Email:
Subject: {email_obj['subject']}
Body: {email_obj['body']}
'''
        try:
            response = self.gemini_model.generate_content(prompt)
            analysis_text = response.text
            logging.info(f"Analyzed email from {email_obj['from']} with subject '{email_obj['subject']}'.")
            json_match = re.search(r'\{[\s\S]*\}', analysis_text)
            if json_match:
                analysis_json = json.loads(json_match.group(0))
            else:
                raise ValueError("No JSON found in Gemini response.")
        except Exception as e:
            logging.error(f"Gemini API error analyzing email from {email_obj['from']}: {e}")
            analysis_json = {
                'intent': 'other',
                'sensitive': True,
                'meeting_time': '',
                'confidence': 'low',
                'raw': ''
            }
        logging.info(f"AI extracted meeting_time: {analysis_json.get('meeting_time', '')}")
        # Add extra log for debugging
        print(f"[DEBUG] Extracted meeting_time: {analysis_json.get('meeting_time', '')}")
        return {
            'intent': analysis_json.get('intent', 'other'),
            'sensitive': analysis_json.get('sensitive', False),
            'meeting_time': analysis_json.get('meeting_time', ''),
            'confidence': analysis_json.get('confidence', 'low'),
            'raw': str(analysis_json)
        }

    def should_reply(self, email_obj, my_email=None):
        if my_email is None:
            my_email = self.email_address  # Use the configured email address
        msg_obj = email_obj.get('msg_obj')
        if not msg_obj:
            return False
        to_list = email.utils.getaddresses(msg_obj.get_all('To', []))
        cc_list = email.utils.getaddresses(msg_obj.get_all('Cc', []))
        all_recipients = [addr.lower() for name, addr in to_list + cc_list]
        my_email = my_email.lower()
        # Debug logging
        logging.info(f"Recipient filter: TO={to_list}, CC={cc_list}, ALL={all_recipients}, MY_EMAIL={my_email}")
        # Allow reply if your email is in To and total recipients is not more than 2
        if my_email in [addr.lower() for name, addr in to_list] and len(all_recipients) <= 2:
            return True
        return False

    def suggest_reply(self, email_obj, analysis, alt_slots=None, first_in_thread=True):
        sender_name = self.extract_sender_name(email_obj)
        greeting = f"Hi {sender_name}, " if first_in_thread else ""
        if analysis['intent'].lower().startswith('meeting'):
            if analysis['meeting_time'] and alt_slots is not None:
                if alt_slots:
                    first_alt = alt_slots[0]
                    reply = f"{greeting}I'm not available at that particular time slot. Can we have the meeting at {first_alt}? Let me know if that works for you, or I can suggest more options."
                else:
                    reply = f"{greeting}I'm not available at the requested time. Please suggest another time."
            else:
                reply = f"{greeting}thank you for your meeting request. Let me check my calendar and get back to you."
        else:
            prompt = f"Write a polite and concise reply to this email. {'Start with ' + greeting if first_in_thread else ''}\nSubject: {email_obj['subject']}\nBody: {email_obj['body']}"
            try:
                response = self.gemini_model.generate_content(prompt)
                reply = response.text.strip()
                if not first_in_thread:
                    # Remove greeting if model adds it
                    reply = re.sub(rf"^{greeting}", "", reply, flags=re.I)
                logging.info(f"Generated reply for email from {email_obj['from']} with subject '{email_obj['subject']}'.")
            except Exception as e:
                logging.error(f"Gemini API error generating reply for email from {email_obj['from']}: {e}")
                reply = f"{greeting}[Error: Could not generate reply]"
        return reply

    def save_dashboard(self, path='dashboard.pkl'):
        with open(path, 'wb') as f:
            pickle.dump({
                'dashboard_data': self.dashboard_data,
                'human_review_queue': self.human_review_queue,
                'threads': self.threads,
                'sent_history': self.sent_history,
                'confirmed_events': self.confirmed_events
            }, f)

    def load_dashboard(self, path='dashboard.pkl'):
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
                self.dashboard_data = data.get('dashboard_data', [])
                self.human_review_queue = data.get('human_review_queue', [])
                self.threads = data.get('threads', {})
                self.sent_history = data.get('sent_history', [])
                self.confirmed_events = data.get('confirmed_events', [])
        except FileNotFoundError:
            pass  # No previous data

    def send_email(self, to_address, subject, body, in_reply_to=None, references=None):
        try:
            # Clean all header fields to avoid linefeed/carriage return errors
            def clean_header(val):
                if val is None:
                    return None
                return str(val).replace('\n', ' ').replace('\r', ' ').strip()
            subject = clean_header(subject)
            to_address = clean_header(to_address)
            from_address = clean_header(self.email_address)
            in_reply_to = clean_header(in_reply_to)
            references = clean_header(references)
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(self.email_address, self.app_password)
                msg = EmailMessage()
                msg['From'] = from_address
                msg['To'] = to_address
                msg['Subject'] = subject
                msg.set_content(body)
                if in_reply_to:
                    msg['In-Reply-To'] = in_reply_to
                if references:
                    msg['References'] = references
                smtp.send_message(msg)
            self.sent_history.append({
                'to': to_address,
                'subject': subject,
                'body': body,
                'time': datetime.datetime.now().isoformat()
            })
            logging.info(f"Sent email to {to_address} with subject '{subject}'.")
        except Exception as e:
            logging.error(f"Error sending email to {to_address}: {e}\n{traceback.format_exc()}")

    def move_to_human_review(self, email_obj, analysis, ai_reply):
        self.human_review_queue.append({
            'uid': email_obj['uid'],
            'from': email_obj['from'],
            'subject': email_obj['subject'],
            'date': email_obj['date'],
            'body': email_obj['body'],
            'intent': analysis['intent'],
            'ai_reply': ai_reply,
            'analysis': analysis,
            'msg_obj': email_obj.get('msg_obj'),
            'thread_id': email_obj.get('thread_id', email_obj['uid'])
        })
        self.move_to_needs_review_label(email_obj['uid'])
        logging.info(f"Moved email from {email_obj['from']} with subject '{email_obj['subject']}' to human review queue and Needs Review label.")

    def create_calendar_event(self, meeting_info):
        try:
            print(f"[DEBUG] Creating calendar event: {meeting_info}")
            start_time = pd.to_datetime(meeting_info['slot'])
            end_time = start_time + pd.Timedelta(hours=1)
            event = {
                'summary': meeting_info['subject'],
                'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Kolkata'},
                'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Kolkata'},
                'attendees': [{'email': email} for email in meeting_info['participants']],
                'description': 'Auto-scheduled via email confirmation.'
            }
            result = self.calendar_service.events().insert(calendarId=self.calendar_id, body=event).execute()
            print(f"[DEBUG] Calendar event created: {result.get('id')}")
            logging.info(f"Created calendar event for {meeting_info['subject']} at {meeting_info['slot']}")
        except Exception as e:
            print(f"[ERROR] Failed to create calendar event: {e}")
            logging.error(f"Failed to create calendar event: {e}")

    def process_inbox(self):
        print("Processing inbox now...")
        emails = self.fetch_emails()
        print(f"Fetched {len(emails)} emails.")
        if not emails:
            logging.info("No new emails to process.")
        for email_obj in emails:
            analysis = self.analyze_email(email_obj)
            thread_id = email_obj.get('thread_id', email_obj['uid'])
            # Handle confirmation intent
            if analysis['intent'] == 'confirmation':
                thank_you_reply = "Okay, thank you! Meet you then."
                msg_obj = email_obj.get('msg_obj')
                in_reply_to = msg_obj.get('Message-ID') if msg_obj else None
                references = msg_obj.get('References') if msg_obj else None
                self.send_email(email_obj['from'], f"Re: {email_obj['subject']}", thank_you_reply, in_reply_to, references)
                self.dashboard_data.append({
                    'uid': email_obj['uid'],
                    'from': email_obj['from'],
                    'subject': email_obj['subject'],
                    'intent': analysis['intent'],
                    'confidence': analysis['confidence'],
                    'sensitive': analysis['sensitive'],
                    'date': email_obj['date'],
                    'ai_reply': thank_you_reply,
                    'status': 'Auto Replied (Confirmation)'
                })
                # If this thread has a pending meeting, add to confirmed_events
                if thread_id in self.pending_meetings:
                    meeting_info = self.pending_meetings.pop(thread_id)
                    self.confirmed_events.append({
                        'subject': meeting_info['subject'],
                        'slot': meeting_info['slot'],
                        'participants': meeting_info['participants'],
                        'confirmed_by': email_obj['from'],
                        'confirmed_at': email_obj['date']
                    })
                    print(f"[DEBUG] Added confirmed event for thread {thread_id}")
                logging.info(f"Confirmation detected and replied to {email_obj['from']}.")
                continue
            # Only process meeting requests
            if analysis['intent'] != 'meeting_request':
                logging.info(f"Skipping email from {email_obj['from']} with intent '{analysis['intent']}'.")
                # Add skipped email to dashboard
                self.dashboard_data.append({
                    'uid': email_obj['uid'],
                    'from': email_obj['from'],
                    'subject': email_obj['subject'],
                    'intent': analysis['intent'],
                    'confidence': analysis['confidence'],
                    'sensitive': analysis['sensitive'],
                    'date': email_obj['date'],
                    'ai_reply': '',
                    'status': 'Skipped'
                })
                continue
            # Only reply if recipient filter passes
            if not self.should_reply(email_obj):
                logging.info(f"Skipping email from {email_obj['from']} due to recipient filter.")
                # Add skipped email to dashboard
                self.dashboard_data.append({
                    'uid': email_obj['uid'],
                    'from': email_obj['from'],
                    'subject': email_obj['subject'],
                    'intent': analysis['intent'],
                    'confidence': analysis['confidence'],
                    'sensitive': analysis['sensitive'],
                    'date': email_obj['date'],
                    'ai_reply': '',
                    'status': 'Skipped'
                })
                continue
            status = ''
            msg_obj = email_obj.get('msg_obj')
            in_reply_to = msg_obj.get('Message-ID') if msg_obj else None
            references = msg_obj.get('References') if msg_obj else None
            # Thread-aware negotiation loop
            if thread_id in self.pending_meetings:
                # This is a follow-up in an ongoing negotiation
                requested_time = analysis.get('meeting_time')
                if requested_time:
                    cal_status, alt_slots = self.check_calendar(requested_time)
                    if cal_status == 'busy':
                        ai_reply = self.suggest_reply(email_obj, analysis, alt_slots)
                        self.send_email(email_obj['from'], f"Re: {email_obj['subject']}", ai_reply, in_reply_to, references)
                        # Update pending meeting with new suggestion
                        if alt_slots:
                            self.pending_meetings[thread_id]['slot'] = alt_slots[0]
                        self.dashboard_data.append({
                            'uid': email_obj['uid'],
                            'from': email_obj['from'],
                            'subject': email_obj['subject'],
                            'intent': analysis['intent'],
                            'confidence': analysis['confidence'],
                            'sensitive': analysis['sensitive'],
                            'date': email_obj['date'],
                            'ai_reply': ai_reply,
                            'status': 'Auto Replied (Busy Slot, Alternatives Sent)'
                        })
                        continue
                    elif cal_status == 'free':
                        ai_reply = self.suggest_reply(email_obj, analysis)
                        self.move_to_human_review(email_obj, analysis, ai_reply)
                        # Update pending meeting with new slot
                        self.pending_meetings[thread_id]['slot'] = requested_time
                        self.dashboard_data.append({
                            'uid': email_obj['uid'],
                            'from': email_obj['from'],
                            'subject': email_obj['subject'],
                            'intent': analysis['intent'],
                            'confidence': analysis['confidence'],
                            'sensitive': analysis['sensitive'],
                            'date': email_obj['date'],
                            'ai_reply': ai_reply,
                            'status': 'Sent for Human Review (Slot Free)'
                        })
                        continue
            # New meeting request or new thread
            if analysis['meeting_time']:
                cal_status, alt_slots = self.check_calendar(analysis['meeting_time'])
                if cal_status == 'busy':
                    ai_reply = self.suggest_reply(email_obj, analysis, alt_slots)
                    self.send_email(email_obj['from'], f"Re: {email_obj['subject']}", ai_reply, in_reply_to, references)
                    # Store the pending meeting
                    if alt_slots:
                        self.pending_meetings[thread_id] = {
                            'slot': alt_slots[0],
                            'subject': email_obj['subject'],
                            'participants': [email_obj['from'], self.email_address]
                        }
                    self.dashboard_data.append({
                        'uid': email_obj['uid'],
                        'from': email_obj['from'],
                        'subject': email_obj['subject'],
                        'intent': analysis['intent'],
                        'confidence': analysis['confidence'],
                        'sensitive': analysis['sensitive'],
                        'date': email_obj['date'],
                        'ai_reply': ai_reply,
                        'status': 'Auto Replied (Busy Slot, Alternatives Sent)'
                    })
                elif cal_status == 'free':
                    ai_reply = self.suggest_reply(email_obj, analysis)
                    self.move_to_human_review(email_obj, analysis, ai_reply)
                    # Store the pending meeting
                    self.pending_meetings[thread_id] = {
                        'slot': analysis['meeting_time'],
                        'subject': email_obj['subject'],
                        'participants': [email_obj['from'], self.email_address]
                    }
                    self.dashboard_data.append({
                        'uid': email_obj['uid'],
                        'from': email_obj['from'],
                        'subject': email_obj['subject'],
                        'intent': analysis['intent'],
                        'confidence': analysis['confidence'],
                        'sensitive': analysis['sensitive'],
                        'date': email_obj['date'],
                        'ai_reply': ai_reply,
                        'status': 'Sent for Human Review (Slot Free)'
                    })
                else:
                    ai_reply = self.suggest_reply(email_obj, analysis)
                    self.move_to_human_review(email_obj, analysis, ai_reply)
                    self.dashboard_data.append({
                        'uid': email_obj['uid'],
                        'from': email_obj['from'],
                        'subject': email_obj['subject'],
                        'intent': analysis['intent'],
                        'confidence': analysis['confidence'],
                        'sensitive': analysis['sensitive'],
                        'date': email_obj['date'],
                        'ai_reply': ai_reply,
                        'status': 'Sent for Human Review (Unknown Calendar Status)'
                    })
            else:
                ai_reply = self.suggest_reply(email_obj, analysis)
                self.move_to_human_review(email_obj, analysis, ai_reply)
                self.dashboard_data.append({
                    'uid': email_obj['uid'],
                    'from': email_obj['from'],
                    'subject': email_obj['subject'],
                    'intent': analysis['intent'],
                    'confidence': analysis['confidence'],
                    'sensitive': analysis['sensitive'],
                    'date': email_obj['date'],
                    'ai_reply': ai_reply,
                    'status': 'Sent for Human Review (No Time Found)'
                })
            # Add to threads
            if thread_id not in self.threads:
                self.threads[thread_id] = []
            self.threads[thread_id].append({
                'from': email_obj['from'],
                'subject': email_obj['subject'],
                'body': email_obj['body'],
                'ai_reply': ai_reply if 'ai_reply' in locals() else '',
                'date': email_obj['date'],
                'status': status if 'status' in locals() else ''
            })
        self.save_dashboard()

    def get_dashboard_data(self):
        return pd.DataFrame(self.dashboard_data)

    def get_human_review_emails(self):
        # Return the human review queue, ensuring each dict has an 'id' key (using 'uid')
        emails = []
        for item in self.human_review_queue:
            email_copy = item.copy()
            email_copy['id'] = email_copy.get('uid')
            emails.append(email_copy)
        return emails

    def get_statistics_data(self):
        df = pd.DataFrame(self.dashboard_data)
        if df.empty:
            return {}, {}, {}
        intent_counts = df['intent'].value_counts().to_dict()
        confidence_counts = df['confidence'].value_counts().to_dict()
        status_counts = df['status'].value_counts().to_dict()
        return intent_counts, confidence_counts, status_counts

    def get_threads_data(self):
        return self.threads

    def human_review_action(self, uid, action, edited_reply=None):
        print(f"[DEBUG] human_review_action called for UID {uid} with action {action}")
        logging.info(f"Human review action called for UID {uid} with action {action}")
        item_to_process = None
        item_index = -1
        for i, item in enumerate(self.human_review_queue):
            if item['uid'] == uid:
                item_to_process = item
                item_index = i
                break

        if item_to_process:
            self.human_review_queue.pop(item_index)  # Remove from queue

            if action == 'send' and edited_reply:
                try:
                    msg_obj = item_to_process.get('msg_obj')
                    in_reply_to = msg_obj.get('Message-ID') if msg_obj else None
                    references = msg_obj.get('References') if msg_obj else None
                    self.send_email(
                        item_to_process['from'],
                        f"Re: {item_to_process['subject']}",
                        edited_reply,
                        in_reply_to=in_reply_to,
                        references=references
                    )
                    print(f"[DEBUG] Sent human-reviewed reply for UID {uid}")
                    logging.info(f"Human review: Sent reply for UID {uid} to {item_to_process['from']}")
                    # Update dashboard
                    for dash_item in self.dashboard_data:
                        if dash_item.get('uid') == uid:
                            dash_item['status'] = 'Human Reviewed - Sent'
                            break
                    # If this thread is pending, add to confirmed_events
                    thread_id = item_to_process.get('thread_id', uid)
                    print(f"[DEBUG] Checking pending_meetings for thread_id: {thread_id}")
                    if hasattr(self, 'pending_meetings') and thread_id in self.pending_meetings:
                        meeting_info = self.pending_meetings.pop(thread_id)
                        print(f"[DEBUG] Found pending meeting for thread_id {thread_id}, adding to confirmed_events...")
                        self.confirmed_events.append({
                            'subject': meeting_info['subject'],
                            'slot': meeting_info['slot'],
                            'participants': meeting_info['participants'],
                            'confirmed_by': item_to_process['from'],
                            'confirmed_at': item_to_process['date']
                        })
                        print(f"[DEBUG] Confirmed event added after human review for thread {thread_id}")
                        logging.info(f"Confirmed event added after human review for thread {thread_id}")
                    else:
                        print(f"[DEBUG] No pending meeting found for thread_id {thread_id}")
                except Exception as e:
                    print(f"[ERROR] Exception in human_review_action send: {e}")
                    logging.error(f"Human review: Failed to send reply for UID {uid}: {e}")

            elif action == 'skip':
                # Update the email's status in the dashboard data to 'Skipped' instead of removing it
                for dash_item in self.dashboard_data:
                    if dash_item.get('uid') == uid:
                        dash_item['status'] = 'Skipped'
                        break
                print(f"[DEBUG] Skipped email UID {uid} and updated dashboard status.")
                logging.info(f"Human review: Skipped email UID {uid}. Updated status to 'Skipped' in dashboard.")

            self.save_dashboard()
        else:
            print(f"[DEBUG] Could not find item with UID {uid} in human review queue for action '{action}'.")
            logging.warning(f"Could not find item with UID {uid} in human review queue for action '{action}'.")

    def update_prompt(self, sender_profile):
        pass

   

    def trigger_list_calendars(self):
        self.list_all_calendars()
        return True

    def get_combined_events(self, calendar_id, max_results=50):
        events = self.list_events_for_calendar(calendar_id, max_results=max_results)
        std_events = []
        for event in events:
            std_events.append({
                'title': event.get('summary', 'No Title'),
                'start': event['start'].get('dateTime', event['start'].get('date')),
                'end': event['end'].get('dateTime', event['end'].get('date')),
                'location': event.get('location', ''),
                'description': event.get('description', '')
            })
        # Ensure all 'start' and 'end' values in std_events are tz-aware UTC
        for event in std_events:
            for col in ['start', 'end']:
                if col in event and event[col]:
                    try:
                        event[col] = pd.to_datetime(event[col], errors='coerce', utc=True)
                    except Exception:
                        pass
        return std_events

    def clear_dashboard(self):
        self.dashboard_data = []
        self.save_dashboard()

    def get_confirmed_events(self):
        return self.confirmed_events