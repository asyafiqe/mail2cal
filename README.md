# 📬 mail2cal

**Turn meeting emails into calendar events — automatically.**

mail2cal monitors your inbox for meeting requests, uses AI to extract event details (title, time, location, description), and creates events in **Google Calendar** and/or **CalDAV-compatible servers** (like Radicale).

Perfect for personal automation, self-hosted setups, or minimizing manual scheduling.

---

## 🌟 Features

- ✅ **AI-Powered Parsing** – Uses LLMs (via OpenRouter) to understand natural language in emails.
- ✅ **Multi-Calendar Support** – Syncs to both Google Calendar and CalDAV.
- ✅ **Self-Host Friendly** – Works with Radicale, Baikal, or any CalDAV server.
- ✅ **Configurable via `.env`** – Easy setup and customization.
- ✅ **Continuous or One-Time Mode** – Run as a daemon or CLI tool.
- ✅ **Smart Date Handling** – Understands phrases like “tomorrow at 3pm” or “next Friday”.
- ✅ **HTML & Plain Text Emails** – Robust body extraction with cleanup.
- ✅ **Docker Ready** – Run in containers easily.

---

## 🚀 Quick Start (Docker) — Recommended

The easiest way to run mail2cal is via **Docker** using the pre-built image.

### 1. Pull and Run with Docker Compose (Recommended)

Create a `docker-compose.yml` file:

```yaml
services:
  mail2cal:
    image: asyafiqe/mail2cal:latest
    container_name: mail2cal
    restart: unless-stopped
    volumes:
      - ./logs:/app/logs:rw
      - ./google_credentials.json:/app/credentials/google_credentials.json:ro
      - ./google_token.json:/app/credentials/google_token.json:rw
    env_file: .env
    # Optional: enable if you want logs visible in real-time
    # logging:
    #   driver: "json-file"
    #   options:
    #     max-size: "10m"
    #     max-file: "3"
```

> 🔹 Make sure `google_credentials.json` and `.env` are in the same directory.

### 2. Create `.env` file

```env
GMAIL_USER=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password

# CalDAV Settings
CALDAV_URL=http://radicale:5232/dav/
CALDAV_USERNAME=username
CALDAV_PASSWORD=password
CALENDAR_NAME=Meetings

# Google Calendar Settings
GOOGLE_CREDENTIALS_FILE=/app/credentials/google_credentials.json
GOOGLE_TOKEN_FILE=/app/credentials/google_token.json
GOOGLE_CALENDAR_NAME=primary

# AI Settings
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=openai/gpt-3.5-turbo
SEARCH_SUBJECT=Meeting Request

# General
TIMEZONE=America/New_York
CHECK_INTERVAL=60
MAX_EMAIL_BODY_CHARS=3000
MARK_AS_PROCESSED=true
```

> ✅ **Note**: Update `TIMEZONE` to your [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), e.g. `Europe/London`.

### 3. Run the service

```bash
docker-compose up -d
```

Check logs:

```bash
docker logs mail2cal
```

To stop:

```bash
docker-compose down
```

> 💡 The container runs continuously, checking your inbox every 60 seconds (configurable).

---

## 🔧 Manual Setup (Without Docker)

### Prerequisites

- Python 3.8+
- Gmail with [App Password](https://myaccount.google.com/apppasswords) enabled
- [OpenRouter.ai](https://openrouter.ai) API key
- Google Calendar API credentials (`google_credentials.json`)
- CalDAV server (optional)

### Install & Run

```bash
git clone https://github.com/asyafiqe/mail2cal.git
cd mail2cal

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

# Create .env and google_credentials.json

python mail2cal.py
```

> Use `RUN_ONCE=true python mail2cal.py` to run once and exit.

---

## 🔐 Security Notes

- 🔒 Never commit `.env`, `google_token.json`, or credentials to git.
- 📂 Use `.gitignore`:
  ```gitignore
  .env
  *.env
  google_token.json
  *.log
  __pycache__/
  *.pyc
  ```
- 🛡️ Use short-lived or restricted OpenRouter API keys with referrer locking.

---

## 📦 Image Details (`asyafiqe/mail2cal`)

- Built from Python 3.11
- Includes required packages: `imaplib`, `caldav`, `google-api-python-client`, `openrouter`, etc.
- Entrypoint runs `python /app/mail2cal.py`
- Working directory: `/app`
- Expected config:
  - `/app/.env` (mounted via `env_file`)
  - `/app/credentials/google_credentials.json` (read-only)
  - `/app/credentials/google_token.json` (writable)

> ✅ Tip: Use volume mounts so tokens persist across container restarts.

---

## 📄 License

MIT © See [LICENSE](LICENSE)

---
