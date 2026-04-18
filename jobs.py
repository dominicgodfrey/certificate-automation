"""Background job processor for certificate generation and email sending.

Runs in a separate thread so the HTTP request returns immediately.
Updates job progress in the database so the frontend can poll for status.
"""
import asyncio
import json
import shutil
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from models import db, Job, SendHistory
from renderer import CertificateRenderer
from emailer import EmailSender, EmailError

ROOT = Path(__file__).parent
KEEP_RECENT_JOB_DIRS = 5


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


def _cleanup_old_job_dirs(keep: int = KEEP_RECENT_JOB_DIRS) -> None:
    """Delete all but the N most-recent `output/job_*` directories.

    Each completed job leaves behind ~N student PDFs on disk. Over many
    batches on a persistent instance this fills the disk. Keep recent
    dirs around so the user can still use /download-all or debug a
    recent send, and drop the rest.
    """
    output_root = ROOT / "output"
    if not output_root.exists():
        return
    try:
        job_dirs = [
            d for d in output_root.iterdir()
            if d.is_dir() and d.name.startswith("job_")
        ]
    except OSError:
        return
    job_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for old_dir in job_dirs[keep:]:
        try:
            shutil.rmtree(old_dir, ignore_errors=True)
        except Exception as e:
            print(f"Warning: failed to clean up {old_dir}: {e}")


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
        email_subject = config.get("email_subject") or \
            f"Your {config.get('program_title', 'Program')} Certificate"
        email_body_template = config.get("email_body") or (
            "Dear $first_name,\n\n"
            f"Congratulations on your successful completion of the "
            f"{config.get('program_title', 'program')}! "
            "Your certificate is attached to this email.\n\n"
            "- The ThinkNeuro Team"
        )

        output_dir = ROOT / "output" / f"job_{job_id}"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Process all students. The EmailSender context manager holds a
        # persistent SMTP connection across the batch and auto-reconnects
        # on drops / transient errors.
        async def do_process():
            async with CertificateRenderer() as renderer:
                with sender:
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

        # Housekeeping: keep only the N most-recent job output directories.
        _cleanup_old_job_dirs()


def start_job(app, job_id: int) -> None:
    """Launch a job in a background thread."""
    thread = threading.Thread(
        target=process_job,
        args=(app, job_id),
        daemon=True,
    )
    thread.start()
