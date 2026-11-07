# WhatsApp Campaign Management Platform 📱

A production-ready WhatsApp broadcast and marketing management platform built with Python, Streamlit, and SQLAlchemy. Features international mobile number validation (ITU-T E.164), Meta-compliant marketing consent management, audience segmentation, broadcast scheduling, and live operational analytics.

---

## 🚀 Key Features

- **📊 Operations Dashboard**: Real-time KPI tracking for message delivery funnel (Sent, Delivered, Read, Failed), conversion rates, and volume performance trends.
- **👥 Contact Management**:
  - CSV audience list ingestion with auto-detected column mapping.
  - International mobile number validation and E.164 normalization (`^\+[1-9]\d{6,14}$`).
  - In-file duplicate screening and database-level duplicate prevention.
- **🛡️ WhatsApp Marketing Consent (Meta Policy Compliance)**:
  - Zero-assumption consent model (uploaded contacts default to Ineligible unless explicit opt-in is verified).
  - Explicit opt-in and opt-out lifecycle handling (e.g., logging STOP keyword requests).
  - Timestamped, auditable consent history with verification sources and proof references.
  - Automated campaign guardrails: strictly excludes non-consented and opted-out contacts from marketing broadcasts.
- **🗄️ Relational Database Architecture**:
  - SQLAlchemy ORM schema supporting SQLite (with WAL mode & foreign keys enabled) and future MySQL migration readiness.
  - 7 relational tables: `contacts`, `consent_records`, `campaigns`, `campaign_recipients`, `message_logs`, `message_status_events`, `link_click_records`.
- **🧪 Rigorous Test Suite**: 27 unit and integration tests covering database persistence, phone normalization, duplicate screening, and consent workflows.

---

## 🛠️ Tech Stack

- **Backend & ORM**: Python 3.12, SQLAlchemy 2.x
- **Web UI**: Streamlit, Altair Charts, Pandas
- **Database**: SQLite (WAL mode, foreign keys enforced) / MySQL ready
- **Testing**: Pytest
- **Scheduling & Webhooks**: APScheduler, Flask, Requests

---

## 📁 Project Structure

```
whatsapp-campaign-platform/
├── app.py                      # Main Streamlit web application entry point
├── config/
│   └── settings.py             # Environment configurations and database settings
├── database/
│   ├── connection.py           # Engine setup, session factory, safe operations
│   └── models.py               # ORM schema models (7 relational tables)
├── modules/
│   ├── dashboard.py            # Operations dashboard with live KPIs and charts
│   ├── contacts.py             # Contact & consent management UI
│   ├── campaigns.py            # Campaign management (in progress)
│   └── analytics.py            # In-depth analytics (in progress)
├── services/
│   ├── contact_service.py      # Validation, E.164 normalization, consent logic
│   ├── campaign_service.py     # Campaign creation, recipient targeting, KPI aggregation
│   ├── link_tracker.py         # URL redirection and click tracking
│   ├── scheduler.py            # APScheduler broadcast jobs
│   └── whatsapp_api.py         # WhatsApp Cloud API client
├── webhooks/
│   └── whatsapp_webhook.py     # Webhook verification and status callbacks
├── data/
│   └── sample_contacts.csv     # Sample contact list for testing
├── tests/
│   ├── test_database.py        # Database schema, persistence & constraint tests
│   ├── test_contacts.py        # Phone validation, duplicate detection, consent tests
│   └── test_campaigns.py       # Campaign targeting & recipient exclusion tests
├── requirements.txt            # Python dependencies
├── pytest.ini                  # Pytest configuration
└── .env.example                # Example environment variables
```

---

## ⚙️ Installation & Setup

### 1. Clone the repository
```bash
git clone https://github.com/Sagnikabhol/WhatsApp-Campaign-Management.git
cd WhatsApp-Campaign-Management
```

### 2. Create and activate a virtual environment
```bash
python -m venv .venv

# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# On Linux / macOS:
source .venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure environment variables
Copy the example environment configuration:
```bash
cp .env.example .env
```
Update `.env` with your WhatsApp Cloud API keys when ready for live broadcasts:
```env
WHATSAPP_API_TOKEN=your_token_here
WHATSAPP_PHONE_NUMBER_ID=your_phone_id_here
WHATSAPP_BUSINESS_ACCOUNT_ID=your_business_id_here
WEBHOOK_VERIFY_TOKEN=your_webhook_verify_token_here
DATABASE_URL=sqlite:///./whatsapp_campaign.db
```

---

## 🚀 Running the Application

Launch the Streamlit web dashboard:
```bash
streamlit run app.py
```
Open **`http://localhost:8501`** in your browser.

---

## 🧪 Running Automated Tests

Run the complete test suite with Pytest:
```bash
pytest -v
```
All 27 test cases will execute across database persistence, phone normalization, duplicate screening, and marketing consent lifecycle rules.

---

## 📄 License
This project is licensed under the MIT License.
