# ThinkNeuro Certificate Automation

Generates personalized PDF certificates from a spreadsheet of students and emails
each one to the corresponding student.

## Setup

1. Install Python dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Copy `.env.example` to `.env` and fill in your SMTP credentials.
   For Gmail, create an App Password at https://myaccount.google.com/apppasswords
   (requires 2FA on the account).

3. Edit `config.yaml` to set certificate content (program title, description,
   signatories, etc.) and email subject/body.

4. Put your student spreadsheet at `data/students.csv` (or update the path in
   `config.yaml`). Required columns: `name`, `email`.

## Running

```
python main.py
```

By default this runs in **dry-run mode**: PDFs are generated but emails are
only printed, not sent. To actually send, set `SEND_EMAILS=true` in `.env`.

Generated PDFs land in `output/certificates/`.

## Architecture

Each stage is its own module so any one piece can be swapped without touching
the others.

- `spreadsheet.py` — reads/validates the student list. Swap CSV for XLSX or
  Google Sheets here.
- `renderer.py` — fills the Jinja2 HTML template and renders to PDF via
  headless Chromium. The template lives at `templates/certificate.html`.
- `emailer.py` — sends each PDF via SMTP. Swap for SendGrid/SES here.
- `main.py` — orchestrator that wires the three together and logs results.
- `config.yaml` — all content that varies between certificate batches. Change
  this to run a different program; no code edits needed.

## Customizing the certificate

The template at `templates/certificate.html` uses Jinja2 placeholders for
every field that varies (`{{ student_name }}`, `{{ program_title }}`, the
`{% for sig in signatories %}` loop, etc.). To support a new certificate
type, you only need to update `config.yaml` — the template handles 2-4
signatories automatically.

The student name auto-shrinks if it's too long for the certificate width.
The fallback minimum is 18pt.

## Future work

- Web UI for non-technical users (upload spreadsheet, preview, send).
- Multiple template support (one HTML file per program type).
- Per-student preview before bulk send.
- Bounce/delivery tracking via SendGrid or similar.
