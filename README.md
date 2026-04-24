# ThinkNeuro Certificate Automation

A web application for generating personalized PDF certificates and emailing
them to students in batch. Built for non-technical staff — pick a template,
upload a spreadsheet, preview, and send with one click.

## Features

- **Multiple certificate templates**: Ships with *Certificate of Completion*
  and *Certificate of Recognition*. A template registry (`template_registry.py`)
  drives which preset fields and signatory fields each layout uses — the UI
  adapts automatically.
- **Template-scoped presets**: Save and reload certificate configurations
  (title, date, description, signatories, email copy) per template. The
  preset dropdown filters to match the selected template so layouts and
  configs never mismatch.
- **CSV / XLSX upload with column mapping**: Upload any spreadsheet and select
  which columns contain the student name and email — no formatting
  requirements. Manual entry works alongside or instead of CSV.
- **Inline certificate preview**: See each rendered certificate as a PNG
  before sending; navigate between students.
- **Background batch processing**: Large batches (400+ students) render and
  email in a background thread with live progress tracking. Close the browser
  and come back — progress is persisted in the database.
- **Resume interrupted sends**: If a job dies mid-batch (host restart, OOM,
  revoked API key), the dashboard surfaces it and a one-click "resend missing"
  flow re-sends only the students who never got their certificate.
- **Email via SendGrid API**: Replaced the earlier SMTP path. A startup
  smoke test catches a revoked/rate-limited API key before an operator
  clicks Send. Per-message jitter (3–5s) keeps batches from looking like a
  spam burst.
- **Login-protected**: Username/password auth with bcrypt-hashed passwords.
  Auto-seeds an admin account on first startup.
- **Dynamic text scaling**: Long student names and cursive signatures
  auto-shrink to fit their container.

## Local Development

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # edit with your SendGrid + admin credentials
python app.py          # runs on http://localhost:5000
```

On first startup, if `DEFAULT_ADMIN_USER` and `DEFAULT_ADMIN_PASSWORD` are set
in `.env` and no users exist yet, an admin account is created automatically.

### CLI mode (no web server)

The original command-line pipeline is still available for one-off renders:

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

### Adding a new certificate template

1. Drop a new Jinja HTML template into `templates/` (use
   `certificate.html` or `certificate_recognition.html` as a starting point;
   see the existing auto-shrink JS pattern).
2. Add an entry to `TEMPLATES` in `template_registry.py` — declare the
   HTML file, which preset fields the template uses, which signatory
   fields are shown, and the max signatories the layout accommodates.
3. That's it. The dashboard picks up the new template automatically:
   selector entry, form-field visibility, preset scoping, signatory caps.
   No changes needed in `app.py`, `jobs.py`, `models.py`, or the dashboard.

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
| `SENDGRID_API_KEY` | (your SendGrid API key) |
| `SENDER_NAME` | `ThinkNeuro` |
| `SENDER_EMAIL` | (your verified sender email) |
| `SEND_EMAILS` | `true` (set to anything else for dry-run mode) |
| `DEFAULT_ADMIN_USER` | `admin` |
| `DEFAULT_ADMIN_PASSWORD` | (choose a strong password) |
| `RENDER_EXTERNAL_URL` | (auto-set by Render — used for keep-alive pings during long batches) |

- Click **Deploy**

### 3. Access the app

Once deployed, Render gives you a URL like
`https://certificate-automation-xxxx.onrender.com`. Log in with the admin
credentials you set above.

### Schema migrations

`db.create_all()` runs on startup and creates missing tables. A lightweight
idempotent migration step in `create_app()` handles in-place column additions
(e.g. `presets.template_id`, which was added when the multi-template feature
shipped). Existing rows are backfilled to `completion` so pre-existing
presets keep rendering the original layout without any manual SQL.

## Architecture

```
app.py                  Flask web application (routes, preset API, renders)
wsgi.py                 Gunicorn entry point
models.py               DB models (User, Preset, Job, SendHistory, PreviewDraft)
jobs.py                 Background job thread (render + email + keep-alive)
renderer.py             HTML → PDF via Playwright/Chromium; template name
                        is parametric
emailer.py              SendGrid API sender with dry-run mode + smoke test
spreadsheet.py          CSV/XLSX reader
template_registry.py    Declarative registry of available certificate
                        templates: file, editable fields, signatory fields,
                        max signatories
main.py                 Legacy CLI entry point
seed_users.py           User management CLI
templates/              Certificate HTML templates + font assets
  certificate.html              Certificate of Completion
  certificate_recognition.html  Certificate of Recognition
web_templates/          Flask page templates (dashboard, preview, job, login)
assets/                 Logos (logo.png, brain_logo.png) and fonts
static/                 CSS + other browser-served assets
config.yaml             Legacy CLI config (presets replace this in web mode)
Dockerfile              Container config for Render
```

Each pipeline stage is its own module. Swap the email provider, database,
or PDF renderer without touching the rest. Adding a new certificate layout
is a template-file + registry-entry change; no code in the render or send
path needs to know about it.

### Multi-template data flow

1. User picks a template on the dashboard. The `template-select` dropdown
   filters the preset dropdown to presets saved with a matching
   `template_id` (carried on each `<option>` as `data-template-id`).
2. Form fields and signatory fields show/hide based on the active
   template's metadata (`TEMPLATES[tid].fields`, `.sig_fields`). The
   signatory "+ Add" button caps at `max_signatories`.
3. On save, `POST /presets/save` persists `template_id` alongside the
   JSON config blob.
4. On preview/send, `template_registry.template_file(config['template_id'])`
   resolves to the HTML file, which `CertificateRenderer(template_name=...)`
   uses — the same Playwright pipeline serves every template.
