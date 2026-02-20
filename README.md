# NJ Transit Delay Alerts - Backend API

Backend service for NJ Transit delay notifications. Provides REST API for subscriptions and background worker for checking train status.

## Features

- 🚂 Real-time train status from NJ Transit API
- 📱 SMS notifications via Twilio (when campaign approved)
- 🔔 Delay, cancellation, and on-time alerts
- 🐳 Dockerized for easy deployment
- 🔐 Secure environment-based configuration

## Tech Stack

- **Python 3.11**
- **FastAPI** - REST API framework
- **SQLite** - Local database
- **NJ Transit Rail API** - Real-time train data
- **Twilio** - SMS notifications
- **Docker** - Containerization

## Prerequisites

- Python 3.11+
- NJ Transit API credentials ([Register here](https://developer.njtransit.com))
- Twilio account ([Sign up here](https://www.twilio.com/try-twilio))
- Docker (for containerized deployment)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/nj-transit-backend.git
cd nj-transit-backend
```

### 2. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and add your credentials:

```env
NJT_USERNAME=your_njtransit_username
NJT_PASSWORD=your_njtransit_password
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_token
TWILIO_PHONE_NUMBER=+19733142062
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run locally

**Start API server:**
```bash
python app.py
```

API available at: http://localhost:8000

**Start background worker (in separate terminal):**
```bash
python worker.py
```

## Docker Deployment

### Build image

```bash
docker build -t nj-transit-backend .
```

### Run container

```bash
docker run -p 8000:8000 \
  -e NJT_USERNAME=your_username \
  -e NJT_PASSWORD=your_password \
  -e TWILIO_ACCOUNT_SID=your_sid \
  -e TWILIO_AUTH_TOKEN=your_token \
  nj-transit-backend
```

## API Endpoints

### Subscribe
```
POST /subscribe
{
  "phone": "+19738208812",
  "morning_train": "3817",
  "evening_train": "3826",
  "delay_alerts": true,
  "ontime_alerts": true
}
```

### Verify
```
POST /verify
{
  "phone": "+19738208812",
  "code": "123456"
}
```

### Unsubscribe
```
POST /unsubscribe
{
  "phone": "+19738208812"
}
```

### Get Trains
```
GET /trains
```

### Get Stats
```
GET /stats
```

## Architecture

```
┌─────────────────┐
│   Frontend      │ (Azure Static Web App)
│   (React/HTML)  │
└────────┬────────┘
         │ HTTPS
         ▼
┌─────────────────┐
│   Backend API   │ (Azure Container Apps)
│   (FastAPI)     │
└────────┬────────┘
         │
    ┌────┴────┬──────────┬───────────┐
    ▼         ▼          ▼           ▼
┌────────┐ ┌──────┐ ┌─────────┐ ┌────────┐
│Database│ │Worker│ │NJ Transit│ │Twilio │
│(SQLite)│ │      │ │   API    │ │  SMS  │
└────────┘ └──────┘ └─────────┘ └────────┘
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `NJT_USERNAME` | NJ Transit API username | Yes |
| `NJT_PASSWORD` | NJ Transit API password | Yes |
| `NJT_API_URL` | API URL (defaults to test) | No |
| `TWILIO_ACCOUNT_SID` | Twilio account SID | For SMS |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | For SMS |
| `TWILIO_PHONE_NUMBER` | Your Twilio number | For SMS |
| `PORT` | Server port (default: 8000) | No |

## Development

### Running tests
```bash
pytest
```

### Code structure
```
├── app.py              # FastAPI application
├── worker.py           # Background scheduler
├── database.py         # Database operations
├── njtransit.py        # NJ Transit API client
├── notifications.py    # SMS service
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker configuration
└── .env.example        # Environment template
```

## Deployment to Azure Container Apps

See [DEPLOY.md](DEPLOY.md) for detailed deployment instructions.

## Security

- ⚠️ **NEVER commit `.env` file to Git**
- ✅ All credentials stored as environment variables
- ✅ `.gitignore` prevents accidental commits
- ✅ Use `.env.example` as template only

## License

MIT

## Contact

Your Name - ketharinath14@gmail.com

Project Link: https://github.com/YOUR_USERNAME/nj-transit-backend
