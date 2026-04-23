"""Background job processor for certificate generation and email sending.

Runs in a separate thread so the HTTP request returns immediately.
Updates job progress in the database so the frontend can poll for status.
"""
import asyncio
import json
import os
import random
import shutil
import time
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from models import db, Job, SendHistory
from renderer import CertificateRenderer
from emailer import EmailSender, EmailError
from template_registry import DEFAULT_TEMPLATE, template_file

ROOT = Path(__file__).parent
KEEP_RECENT_JOB_DIRS = 5

# Per-message pacing. Slower + jittered sends look less like a spam burst
# to receiving servers and stay well under typical SMTP throttle limits.
# At 4s avg and 400 students this is a ~27 min batch, which is the right
# trade-off for tomorrow's send.
SEND_DELAY_MIN_SECONDS = 3.0
SEND_DELAY_MAX_SECONDS = 5.0

# Restart the Playwright browser every N students to prevent Chromium's
# memory from accumulating over a long batch and triggering an OOM kill
# on memory-constrained hosts (e.g. Render free tier at 512MB).
RENDERER_RESTART_EVERY = 100

# Keep-alive: free PaaS tiers (Render free, Fly hobby) spin the web
# instance down after ~15 min of no incoming HTTP traffic. A background
# job thread does NOT count as traffic, so a long batch will get its
# instance killed mid-send. We hit our own /healthz from a side thread
# every ~10 min while a job is active to keep the instance hot.
KEEPALIVE_INTERVAL_SECONDS = 600  # 10 min, well under the 15-min sleep
KEEPALIVE_PATH = "/healthz"


def _start_keepalive(stop_event: threading.Event) -> threading.Thread | None:
    """Spawn a thread that pings our own public URL until stop_event is set.

    Only active when RENDER_EXTERNAL_URL (or KEEPALIVE_URL) is configured;
    otherwise this is a no-op so local dev doesn't try to call out.
    Failures are swallowed — keep-alive must never crash a send.
    """
    base = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        return None
    url = base.rstrip("/") + KEEPALIVE_PATH

    def loop():
        while not stop_event.is_set():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "cert-keepalive/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read(64)
            except Exception as e:
                print(f"keepalive ping failed (non-fatal): {e}")
            # Sleep in short chunks so we exit promptly when stop_event fires.
            for _ in range(KEEPALIVE_INTERVAL_SECONDS):
                if stop_event.is_set():
                    return
                time.sleep(1)

    t = threading.Thread(target=loop, daemon=True, name="job-keepalive")
    t.start()
    return t


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

        # Build email template. The default subject now includes the
        # student's first name — varied subjects look much less like a
        # spam blast (which typically reuses one subject for the whole
        # batch) and meaningfully improve inbox placement.
        # Recognition templates don't have a program_title, so fall back
        # to a generic phrase when it's missing.
        program_label = config.get("program_title") or "ThinkNeuro"
        email_subject_template = config.get("email_subject") or \
            f"$first_name, your {program_label} Certificate"
        email_body_template = config.get("email_body") or (
            "Dear $first_name,\n\n"
            f"Congratulations! "
            "Your certificate is attached to this email.\n\n"
            "- The ThinkNeuro Team"
        )

        tpl_file = template_file(config.get("template_id", DEFAULT_TEMPLATE))

        output_dir = ROOT / "output" / f"job_{job_id}"
        output_dir.mkdir(parents=True, exist_ok=True)

        async def do_process():
            with sender:
                for chunk_start in range(0, len(students), RENDERER_RESTART_EVERY):
                    chunk = students[chunk_start:chunk_start + RENDERER_RESTART_EVERY]
                    async with CertificateRenderer(template_name=tpl_file) as renderer:
                        for j, student in enumerate(chunk):
                            i = chunk_start + j
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
                                subject = Template(email_subject_template).safe_substitute(
                                    first_name=first_name)
                                body = Template(email_body_template).safe_substitute(
                                    first_name=first_name)
                                sender.send(
                                    to=student["email"],
                                    subject=subject,
                                    body=body,
                                    attachment_path=pdf_path,
                                )
                                status = "sent"

                                # Rate limit between sends. Jittered (3–5s by
                                # default) so the cadence doesn't look mechanical
                                # — a constant 2s gap is itself a spam signal.
                                if not sender.dry_run and i < len(students) - 1:
                                    time.sleep(random.uniform(
                                        SEND_DELAY_MIN_SECONDS,
                                        SEND_DELAY_MAX_SECONDS))

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

        # Spin up the self-ping keep-alive for the duration of this job
        # so a long batch (~30 min for 400 students) doesn't get killed
        # by free-tier idle-sleep. No-op outside Render / when the URL
        # env var isn't set.
        keepalive_stop = threading.Event()
        keepalive_thread = _start_keepalive(keepalive_stop)

        try:
            asyncio.run(do_process())
            job.status = "complete"
        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
        finally:
            keepalive_stop.set()
            if keepalive_thread is not None:
                keepalive_thread.join(timeout=2)

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
