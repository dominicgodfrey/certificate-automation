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
  email in a background thread with live progress tracking.
- **Resume interrupted sends**: If a job fails mid-batch (revoked API key,
  bounced addresses), the dashboard surfaces it and a one-click "resend
  missing" flow re-sends only the students who never got their certificate.
- **No database — results CSVs are the record**: Student PII never persists
  server-side. Every batch writes a per-student results CSV (name, email,
  sent/failed) that is downloadable from the job page and emailed to the
  operator inbox as checkpoint reports (every 100 students) plus a final
  report. After a server restart, re-uploading a results CSV on the
  dashboard automatically excludes already-sent students — no duplicates.
- **Email via SendGrid API**: Replaced the earlier SMTP path. A startup
  smoke test catches a revoked/rate-limited API key before an operator
  clicks Send. Per-message jitter (3–5s) keeps batches from looking like a
  spam burst.
- **Login-protected**: Single operator account defined by
  `DEFAULT_ADMIN_USER` / `DEFAULT_ADMIN_PASSWORD` env vars — no user
  database to seed or manage.
- **Dynamic text scaling**: Long student names and cursive signatures
  auto-shrink to fit their container.

## Local Development

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # edit with your SendGrid + admin credentials
python app.py          # runs on http://localhost:5000
```

Log in with `DEFAULT_ADMIN_USER` / `DEFAULT_ADMIN_PASSWORD` from `.env` —
these are the live credentials (login is disabled until the password is set).

### CLI mode (no web server)

The original command-line pipeline is still available for one-off renders:

```bash
python main.py                  # uses config.yaml
python main.py --config foo.yaml
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
   No changes needed in `app.py`, `jobs.py`, or the dashboard.

## Deploy to Render

No database is needed. Create a Web Service:

- Click **New > Web Service**
- Connect your GitHub repo
- Select **Docker** as the environment
- Set the following environment variables:

| Variable | Value |
|----------|-------|
| `FLASK_SECRET_KEY` | (run `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `SENDGRID_API_KEY` | (your SendGrid API key) |
| `SENDER_NAME` | `ThinkNeuro` |
| `SENDER_EMAIL` | (your verified sender email) |
| `SEND_EMAILS` | `true` (set to anything else for dry-run mode) |
| `RESULTS_EMAIL` | (optional — where batch results reports go; defaults to `SENDER_EMAIL`) |
| `DEFAULT_ADMIN_USER` | `admin` |
| `DEFAULT_ADMIN_PASSWORD` | (choose a strong password — these are the live login credentials) |
| `RENDER_EXTERNAL_URL` | (auto-set by Render — used for keep-alive pings during long batches) |
| `GITHUB_TOKEN` | (optional — enables durable UI presets, see below) |

- Click **Deploy**

Once deployed, Render gives you a URL like
`https://certificate-automation-xxxx.onrender.com`. Log in with the admin
credentials you set above.

### Storage model (what survives what)

| Data | Where it lives | Survives restart/redeploy? |
|------|----------------|---------------------------|
| Presets in `presets.json` | Git repo, ships with each deploy | Yes |
| Presets saved via the UI | Instance disk overlay | With `GITHUB_TOKEN` set: yes — auto-committed to `presets.json`. Without: no — use **Export** to promote one by hand |
| Job progress / send history | Process memory | No — the results CSV (downloaded or emailed) is the durable record |
| Student names/emails | Memory during the batch only | Never persists server-side (by design) |

### Durable presets from the UI (recommended)

Set `GITHUB_TOKEN` to make presets created or edited in the web UI
permanent with zero manual steps: the app commits the updated
`presets.json` straight to the repo via the GitHub API. The commit
message carries `[skip render]`, so Render does **not** redeploy (the
running instance already serves the preset from its local overlay; the
commit just guarantees the next deploy ships it too).

Token setup: GitHub → Settings → Developer settings →
[Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens)
→ Generate new token → Repository access: **only this repo** →
Permissions: **Contents: Read and write**. Paste it into Render as
`GITHUB_TOKEN`. If the token is missing or expired, preset saves still
work on the running instance — the save toast warns that the preset
won't survive a redeploy.

### Recovering an interrupted batch

1. If the app is still up and the job shows **failed**: click
   **Re-send missing only** — no CSV needed.
2. If the server restarted mid-batch: find the latest checkpoint/final
   report email (sent to `RESULTS_EMAIL`), download its `results.csv`,
   and upload it on the dashboard like a normal student list. Rows
   marked `sent` are excluded automatically; preview and send the rest.
3. Last resort with no CSV: Render's log stream prints one
   `RESULT job=N i/total sent|failed name <email>` line per student —
   reconstruct the sent list from there.

## Architecture

```
app.py                  Flask web application (routes, preset API, renders)
wsgi.py                 Gunicorn entry point
store.py                Storage: file-backed presets + in-memory jobs/drafts
presets.json            Repo-committed presets (durable across deploys)
jobs.py                 Background job thread (render + email + keep-alive
                        + results CSV / checkpoint reports)
renderer.py             HTML → PDF via Playwright/Chromium; template name
                        is parametric
emailer.py              SendGrid API sender with dry-run mode + smoke test
spreadsheet.py          CSV/XLSX reader
template_registry.py    Declarative registry of available certificate
                        templates: file, editable fields, signatory fields,
                        max signatories
main.py                 Legacy CLI entry point
templates/              Certificate HTML templates + font assets
  certificate.html              Certificate of Completion
  certificate_recognition.html  Certificate of Recognition
web_templates/          Flask page templates (dashboard, preview, job, login)
assets/                 Logos (logo.png, brain_logo.png) and fonts
static/                 CSS + other browser-served assets
config.yaml             Legacy CLI config (presets replace this in web mode)
Dockerfile              Container config for Render
```

Each pipeline stage is its own module. Swap the email provider, storage,
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
