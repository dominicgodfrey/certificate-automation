# ThinkNeuro Certificate Automation

A web application for generating personalized PDF certificates and emailing
them to students in batch. Built for non-technical staff — upload a spreadsheet,
preview certificates, and send with one click.

## Features

- **Template presets**: Save and load certificate configurations (program title,
  date, hours, signatories) for reuse across programs.
- **CSV upload with column mapping**: Upload any spreadsheet and select which
  columns contain the student name and email — no formatting requirements.
- **Manual student entry**: Add individual students alongside or instead of CSV.
- **Certificate preview**: See rendered certificates inline before sending.
  Navigate between students and search by name.
- **Background batch processing**: Large batches (400+ students) process in the
  background with live progress tracking. Close the browser and come back later.
- **Login-protected**: Username/password authentication with bcrypt-hashed
  passwords. Auto-seeds an admin account on first startup.
- **Dynamic text scaling**: Long student names automatically shrink to fit.

## Local Development

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # edit with your SMTP credentials
python app.py          # runs on http://localhost:5000
```

On first startup with `DEFAULT_ADMIN_USER` and `DEFAULT_ADMIN_PASSWORD` set in
`.env`, an admin account is created automatically.

### CLI mode (no web server)

The original command-line pipeline is still available:

```bash
python main.py                  # uses config.yaml
python main.py --config foo.yaml
```

### Managing users

```bash
python seed_users.py add <username> <password>
python seed_users.py list
python seed_users.py remove <username>
```

## Deploy to Render

### 1. Create a PostgreSQL database

- On Render, click **New > PostgreSQL**
- Free tier is fine
- Note the **Internal Database URL** after creation

### 2. Create a Web Service

- Click **New > Web Service**
- Connect your GitHub repo
- Select **Docker** as the environment
- Set the following environment variables:

| Variable | Value |
|----------|-------|
| `DATABASE_URL` | (paste Internal Database URL from step 1) |
| `FLASK_SECRET_KEY` | (run `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | (your sender email) |
| `SMTP_PASSWORD` | (your app password) |
| `SENDER_NAME` | `ThinkNeuro` |
| `SENDER_EMAIL` | (your sender email) |
| `SEND_EMAILS` | `true` |
| `DEFAULT_ADMIN_USER` | `admin` |
| `DEFAULT_ADMIN_PASSWORD` | (choose a strong password) |

- Click **Deploy**

### 3. Access the app

Once deployed, Render gives you a URL like
`https://certificate-automation-xxxx.onrender.com`. Log in with the admin
credentials you set above.

## Architecture

```
app.py              Flask web application
wsgi.py             Gunicorn entry point
models.py           Database models (User, Preset, Job, SendHistory)
jobs.py             Background job processor
renderer.py         HTML → PDF via Playwright/Chromium
emailer.py          SMTP email sender
spreadsheet.py      CSV/XLSX reader
main.py             Original CLI entry point
seed_users.py       User management CLI
templates/          Certificate HTML template + fonts
web_templates/      Flask page templates
static/             Logo and static assets
config.yaml         CLI configuration (presets replace this in web mode)
Dockerfile          Container configuration
```

Each pipeline stage is its own module. Swap the email provider, database, or
PDF renderer without touching the rest.
