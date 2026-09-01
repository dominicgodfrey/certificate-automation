"""Registry of certificate templates available in the system.

Single source of truth for:
- Which HTML template file each template id maps to
- Which preset config fields a template actually uses (drives dashboard
  form visibility)
- Which signatory fields a template supports (and which are required)
- Max signatories the template's layout can accommodate

Adding a new template = add a new entry here + ship the HTML file.
"""

TEMPLATES = {
    "completion": {
        "label": "Certificate of Completion",
        "file": "certificate.html",
        "fields": {
            "program_title": True,
            "date": True,
            "hours": True,
            "program_description": True,
            "footer": True,
            "email_subject": True,
            "email_body": True,
        },
        "sig_fields": ["cursive", "name", "degrees", "institution"],
        "sig_required": ["cursive", "name"],
        "max_signatories": 4,
    },
    "completion_v2": {
        "label": "Certificate of Completion (v2)",
        "file": "certificate_completion_v2.html",
        "fields": {
            "program_title": True,
            "date": True,
            "hours": True,
            "program_description": True,
            "footer": True,
            "email_subject": True,
            "email_body": True,
        },
        "sig_fields": ["cursive", "name", "degrees", "institution"],
        "sig_required": ["cursive", "name"],
        "max_signatories": 4,
    },
    "recognition_v2": {
        "label": "Certificate of Recognition (v2)",
        "file": "certificate_recognition_v2.html",
        "fields": {
            "program_description": True,
            "footer": True,
            "email_subject": True,
            "email_body": True,
        },
        "sig_fields": ["cursive", "name", "title", "degrees", "institution"],
        "sig_required": ["cursive", "name", "title"],
        "max_signatories": 2,
    },
    "recognition": {
        "label": "Certificate of Recognition",
        "file": "certificate_recognition.html",
        "fields": {
            "program_description": True,
            "email_subject": True,
            "email_body": True,
        },
        "sig_fields": ["cursive", "name", "title", "degrees", "institution"],
        "sig_required": ["cursive", "name", "title"],
        "max_signatories": 2,
    },
}

DEFAULT_TEMPLATE = "completion"


def template_file(template_id: str) -> str:
    """Return the HTML file name for a template id, falling back to default."""
    entry = TEMPLATES.get(template_id) or TEMPLATES[DEFAULT_TEMPLATE]
    return entry["file"]
