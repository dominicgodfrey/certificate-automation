"""Background job processor for certificate generation and email sending.

Runs in a separate thread so the HTTP request returns immediately.
Updates job progress in the database so the frontend can poll for status.
"""
import asyncio
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from models import db, Job, SendHistory
from renderer import CertificateRenderer
from emailer import EmailSender, EmailError

ROOT = Path(__file__).parent


def _build_render_data(student: dict, config: dict) -> dict:
    return {
        "student_name": student["name"],
        "date": config.get("date", ""),
        "program_title": config.get("program_title", ""),
        "program_description": config.get("program_description", ""),
        "hours": config.get("hours", ""),
        "footer": config.get("footer", ""),
        "signatories": config.get("signatories", []),
    }


def process_job(app, job_id: int) -> None:
    """Process a certificate send job in a background thread.

    Uses the Flask app context to access the database.
    """
    with app.app_context():
        job = db.session.get(Job, job_id)
        if not job:
            return

        config = json.loads(job.config_json)
        students = json.loads(job.students_json)

        # Mark as running
        job.status = "running"
        db.session.commit()

        # Set up email sender
        try:
            sender = EmailSender.from_env()
        except EmailError as e:
            job.status = "failed"
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            return

        # Build email template
        email_subject = config.get("email_subject",
            f"Your {config.get('program_title', 'Program')} Certificate")
        email_body_template = config.get("email_body",
            "Dear $first_name,\n\n"
            f"Congratulations on your successful completion of the "
            f"{config.get('program_title', 'program')}! "
            "Your certificate is attached to this email.\n\n"
            "- The ThinkNeuro Team")

        output_dir = ROOT / "output" / f"job_{job_id}"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Process all students
        async def do_process():
            async with CertificateRenderer() as renderer:
                for i, student in enumerate(students):
                    status = "pending"
                    error = None

                    try:
                        # Render PDF
                        slug = "".join(c if c.isalnum() else "_"
                                       for c in student["name"]).strip("_")
                        pdf_path = output_dir / f"certificate_{slug}.pdf"
                        await renderer.render(
                            _build_render_data(student, config), pdf_path)

                        # Send email
                        first_name = student["name"].split()[0]
                        body = Template(email_body_template).safe_substitute(
                            first_name=first_name)
                        sender.send(
                            to=student["email"],
                            subject=email_subject,
                            body=body,
                            attachment_path=pdf_path,
                        )
                        status = "sent"

                        # Rate limit between sends
                        if not sender.dry_run and i < len(students) - 1:
                            time.sleep(2)

                    except Exception as e:
                        status = "failed"
                        error = str(e)

                    # Record this student's result
                    history = SendHistory(
                        student_name=student["name"],
                        student_email=student["email"],
                        preset_name=config.get("program_title", ""),
                        status=status,
                        error_message=error,
                        job_id=job_id,
                    )
                    db.session.add(history)

                    # Update job progress
                    job.processed_count = i + 1
                    db.session.commit()

        try:
            asyncio.run(do_process())
            job.status = "complete"
        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)

        job.completed_at = datetime.now(timezone.utc)
        db.session.commit()


def start_job(app, job_id: int) -> None:
    """Launch a job in a background thread."""
    thread = threading.Thread(
        target=process_job,
        args=(app, job_id),
        daemon=True,
    )
    thread.start()
