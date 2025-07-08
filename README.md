# Automatic Email Reply System with Agentic AI

## Features
- Gmail integration (IMAP/SMTP)
- Google Calendar integration
- Gemini API for AI-powered replies
- Human-in-the-loop review dashboard
- Meeting scheduling and auto-replies

## Setup

1. **Clone the repository**
2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
3. **Google Calendar API**
   - Place your `credentials.json` file in the project root directory.
   - Ensure your Google account has Calendar API enabled.
4. **Gmail App Password**
   - Use your Gmail app password for IMAP/SMTP access.
5. **Environment Variables**
   - Create a `.env` file with the following:
     ```env
     EMAIL_ADDRESS=your_email@gmail.com
     EMAIL_APP_PASSWORD=your_app_password
     ```
6. **Run the App**
   ```bash
   streamlit run app.py
   ```

## Usage
- The dashboard will show incoming emails, intents, and allow human review for sensitive/meeting-related emails.
- Automatic replies are sent for high-confidence, non-sensitive emails. 