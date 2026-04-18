"""ThinkNeuro Certificate Automation — Web Application."""
import json
import os
from pathlib import Path

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import db, User, Preset, PreviewDraft

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "web_templates"),
        static_folder=str(ROOT / "static"),
    )

    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

    # Use DATABASE_URL for production (PostgreSQL on Render),
    # fall back to SQLite for local development.
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'app.db'}")
    # Render provides postgres:// but SQLAlchemy requires postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Log the DB host (no password) at startup so it's obvious on Render
    # which database is wired up — Supabase, Render PG, or local SQLite —
    # and easy to spot if the env var ever points somewhere unexpected.
    try:
        from urllib.parse import urlparse
        _u = urlparse(database_url)
        _backend = _u.scheme.split("+")[0]
        _host = _u.hostname or "(local)"
        print(f"DB backend={_backend} host={_host}")
    except Exception:
        pass

    db.init_app(app)
    with app.app_context():
        db.create_all()

        # Auto-seed admin on first startup if no users exist
        if User.query.count() == 0:
            admin_user = os.environ.get("DEFAULT_ADMIN_USER", "admin")
            admin_pass = os.environ.get("DEFAULT_ADMIN_PASSWORD", "")
            if admin_pass:
                pw_hash = bcrypt.hashpw(
                    admin_pass.encode("utf-8"), bcrypt.gensalt()
                ).decode("utf-8")
                db.session.add(User(username=admin_user, password_hash=pw_hash))
                db.session.commit()
                print(f"Auto-seeded admin user: {admin_user}")

        # Stuck-job sweep: if the worker was killed (Render restart, deploy,
        # OOM) while a job was running, it is permanently stuck in 'running'
        # with no thread to finish it. Mark any such jobs as failed so the
        # user sees a clear state and can re-send.
        from datetime import datetime, timezone
        from models import Job
        stuck = Job.query.filter_by(status="running").all()
        if stuck:
            now = datetime.now(timezone.utc)
            for j in stuck:
                j.status = "failed"
                j.error_message = ("Server restarted while job was running. "
                                   "Please re-send the batch.")
                j.completed_at = now
            db.session.commit()
            print(f"Marked {len(stuck)} stuck job(s) as failed on startup.")

        # Loud-but-non-fatal SMTP credential smoke test. Workspace policy
        # changes silently revoke App Passwords; we want that to show up
        # in deploy logs *before* an operator clicks Send and watches
        # 400 students fail in a row.
        try:
            from emailer import EmailSender
            ok, msg = EmailSender.smoke_test()
            print(("SMTP OK: " if ok else "SMTP WARNING: ") + msg)
        except Exception as e:
            print(f"SMTP smoke test crashed (non-fatal): {e}")

    # --- Login manager ---
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = ""  # suppress the default "please log in" flash

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # --- Helpers ---

    def _get_preview_draft():
        """Return the current user's active PreviewDraft, or None."""
        draft_id = session.get("preview_draft_id")
        if not draft_id:
            return None
        draft = db.session.get(PreviewDraft, draft_id)
        # Guard against a user reusing a session after the draft was deleted
        # or the cookie pointing at another user's draft.
        if draft is None or draft.user_id != current_user.id:
            return None
        return draft

    # --- Routes ---

    @app.route("/healthz", methods=["GET"])
    def healthz():
        """Liveness probe + keep-alive target.

        The background job thread pings this from within the same instance
        during long batches so the host platform sees continuous HTTP
        traffic and doesn't spin the web instance down mid-send. No auth,
        no DB hit — must stay cheap.
        """
        return "ok", 200

    @app.route("/", methods=["GET"])
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            user = User.query.filter_by(username=username).first()
            if user and bcrypt.checkpw(password.encode("utf-8"),
                                       user.password_hash.encode("utf-8")):
                login_user(user)
                return redirect(url_for("dashboard"))
            else:
                flash("Invalid username or password.", "error")

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/dashboard", methods=["GET"])
    @login_required
    def dashboard():
        from models import Job, SendHistory
        presets = Preset.query.order_by(Preset.name).all()
        # Load last-used preset from session, or default to empty
        active_preset_id = session.get("active_preset_id")
        active_config = None
        if active_preset_id:
            preset = db.session.get(Preset, active_preset_id)
            if preset:
                active_config = json.loads(preset.config_json)

        # Surface any of THIS user's failed jobs that still have students
        # missing a successful send. Lets the operator finish an
        # interrupted batch from the dashboard instead of having to
        # remember the URL of the failed job page.
        unfinished = []
        recent_failed = (
            Job.query
            .filter(Job.created_by == current_user.id)
            .filter(Job.status == "failed")
            .order_by(Job.created_at.desc())
            .limit(5)
            .all()
        )
        for j in recent_failed:
            try:
                total = j.total_students or 0
                sent = SendHistory.query.filter_by(
                    job_id=j.id, status="sent").count()
                missing = total - sent
                if missing > 0:
                    unfinished.append({
                        "id": j.id,
                        "total": total,
                        "sent": sent,
                        "missing": missing,
                        "created_at": j.created_at,
                    })
            except Exception:
                continue

        return render_template("dashboard.html",
                               presets=presets,
                               active_preset_id=active_preset_id,
                               active_config=active_config,
                               unfinished_jobs=unfinished)

    # --- Preset API routes ---

    @app.route("/presets/load/<int:preset_id>", methods=["GET"])
    @login_required
    def load_preset(preset_id):
        preset = db.session.get(Preset, preset_id)
        if not preset:
            return jsonify({"error": "Preset not found"}), 404
        session["active_preset_id"] = preset_id
        return jsonify({"config": json.loads(preset.config_json), "name": preset.name})

    @app.route("/presets/save", methods=["POST"])
    @login_required
    def save_preset():
        data = request.get_json()
        name = data.get("name", "").strip()
        config = data.get("config")

        if not name or not config:
            return jsonify({"error": "Name and config are required"}), 400

        existing = Preset.query.filter_by(name=name).first()
        if existing:
            existing.config_json = json.dumps(config)
            db.session.commit()
            session["active_preset_id"] = existing.id
            return jsonify({"message": f"Updated '{name}'", "id": existing.id})
        else:
            preset = Preset(name=name, config_json=json.dumps(config))
            db.session.add(preset)
            db.session.commit()
            session["active_preset_id"] = preset.id
            return jsonify({"message": f"Saved '{name}'", "id": preset.id})

    @app.route("/presets/delete/<int:preset_id>", methods=["DELETE"])
    @login_required
    def delete_preset(preset_id):
        preset = db.session.get(Preset, preset_id)
        if not preset:
            return jsonify({"error": "Preset not found"}), 404
        name = preset.name
        db.session.delete(preset)
        db.session.commit()
        if session.get("active_preset_id") == preset_id:
            session.pop("active_preset_id", None)
        return jsonify({"message": f"Deleted '{name}'"})

    # --- CSV upload ---

    @app.route("/upload-csv", methods=["POST"])
    @login_required
    def upload_csv():
        """Parse an uploaded CSV/XLSX and return headers + rows as JSON."""
        import pandas as pd
        import io

        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No file uploaded"}), 400

        filename = file.filename.lower()
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(io.BytesIO(file.read()))
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(io.BytesIO(file.read()))
            else:
                return jsonify({"error": "Unsupported file type. Use CSV or XLSX."}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to parse file: {e}"}), 400

        # Clean up: strip whitespace from headers and convert NaN to empty string
        df.columns = [str(c).strip() for c in df.columns]
        df = df.fillna("")

        headers = list(df.columns)
        rows = df.astype(str).values.tolist()

        return jsonify({"headers": headers, "rows": rows})

    # --- Generate preview ---

    @app.route("/generate-preview", methods=["POST"])
    @login_required
    def generate_preview():
        """Persist certificate config + student list as a PreviewDraft.

        Stored server-side (not in the session cookie) so that large
        batches — up to 1000+ students — don't exceed the ~4KB signed
        cookie limit and silently fail.
        """
        data = request.get_json()
        config = data.get("config")
        students = data.get("students")  # list of {name, email}

        if not config:
            return jsonify({"error": "Certificate settings are required"}), 400
        if not students or len(students) == 0:
            return jsonify({"error": "At least one student is required"}), 400

        # Validate students
        issues = []
        for i, s in enumerate(students):
            if not s.get("name", "").strip():
                issues.append(f"Row {i+1}: missing name")
            if not s.get("email", "").strip() or "@" not in s.get("email", ""):
                issues.append(f"Row {i+1}: missing or invalid email")
        if issues:
            return jsonify({"error": "Student data issues:\n" + "\n".join(issues)}), 400

        # Drop any prior drafts for this user so the table doesn't accumulate
        # abandoned previews.
        PreviewDraft.query.filter_by(user_id=current_user.id).delete()

        draft = PreviewDraft(
            user_id=current_user.id,
            config_json=json.dumps(config),
            students_json=json.dumps(students),
        )
        db.session.add(draft)
        db.session.commit()

        session["preview_draft_id"] = draft.id
        # Clear any stale cookie-based preview data from before this change.
        session.pop("preview_config", None)
        session.pop("preview_students", None)

        return jsonify({"redirect": url_for("preview")})

    # --- Preview page ---

    @app.route("/preview")
    @login_required
    def preview():
        draft = _get_preview_draft()
        if draft is None:
            flash("No data to preview. Please fill out the form first.", "error")
            return redirect(url_for("dashboard"))
        config = json.loads(draft.config_json)
        students = json.loads(draft.students_json)
        return render_template("preview.html", config=config, students=students)

    # --- Certificate rendering for preview ---

    @app.route("/preview/render/<int:student_index>")
    @login_required
    def render_student_certificate(student_index):
        """Render a single student's certificate as PDF and return it."""
        import asyncio
        from renderer import CertificateRenderer

        draft = _get_preview_draft()
        if draft is None:
            return "No preview data", 400
        config = json.loads(draft.config_json)
        students = json.loads(draft.students_json)
        if student_index < 0 or student_index >= len(students):
            return "Invalid student index", 400

        student = students[student_index]
        render_data = {
            "student_name": student["name"],
            "date": config.get("date", ""),
            "program_title": config.get("program_title", ""),
            "program_description": config.get("program_description", ""),
            "hours": config.get("hours", ""),
            "footer": config.get("footer", ""),
            "signatories": config.get("signatories", []),
        }

        output_dir = ROOT / "output" / "preview"
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / f"preview_{student_index}.pdf"

        async def do_render():
            async with CertificateRenderer() as renderer:
                await renderer.render(render_data, pdf_path)

        asyncio.run(do_render())

        from flask import send_file
        return send_file(str(pdf_path), mimetype="application/pdf",
                         download_name=f"certificate_{student['name']}.pdf")

    @app.route("/preview/render-image/<int:student_index>")
    @login_required
    def render_student_image(student_index):
        """Render a single student's certificate as PNG for inline display."""
        import asyncio
        from playwright.async_api import async_playwright
        from jinja2 import Environment, FileSystemLoader
        from renderer import CertificateRenderer, TEMPLATES

        draft = _get_preview_draft()
        if draft is None:
            return "No preview data", 400
        config = json.loads(draft.config_json)
        students = json.loads(draft.students_json)
        if student_index < 0 or student_index >= len(students):
            return "Invalid student index", 400

        student = students[student_index]
        render_data = {
            "student_name": student["name"],
            "date": config.get("date", ""),
            "program_title": config.get("program_title", ""),
            "program_description": config.get("program_description", ""),
            "hours": config.get("hours", ""),
            "footer": config.get("footer", ""),
            "signatories": config.get("signatories", []),
        }

        output_dir = ROOT / "output" / "preview"
        output_dir.mkdir(parents=True, exist_ok=True)
        png_path = output_dir / f"preview_{student_index}.png"

        async def do_render():
            env = Environment(
                loader=FileSystemLoader(str(TEMPLATES)),
                autoescape=True,
            )
            template = env.get_template("certificate.html")
            html = template.render(**render_data)
            tmp_path = TEMPLATES / f"_preview_{student_index}.html"
            tmp_path.write_text(html, encoding="utf-8")
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    page = await browser.new_page(
                        viewport={"width": 1100, "height": 850},
                        device_scale_factor=2,
                    )
                    await page.goto(tmp_path.as_uri())
                    await page.wait_for_load_state("networkidle")
                    await page.evaluate("document.fonts.ready")
                    await page.wait_for_timeout(200)
                    await page.screenshot(path=str(png_path))
                    await page.close()
                    await browser.close()
            finally:
                tmp_path.unlink(missing_ok=True)

        asyncio.run(do_render())

        from flask import send_file
        return send_file(str(png_path), mimetype="image/png")

    # --- Helper: build render data for a student ---

    def _build_render_data(student, config):
        return {
            "student_name": student["name"],
            "date": config.get("date", ""),
            "program_title": config.get("program_title", ""),
            "program_description": config.get("program_description", ""),
            "hours": config.get("hours", ""),
            "footer": config.get("footer", ""),
            "signatories": config.get("signatories", []),
        }

    # --- Send certificates (via background job) ---

    @app.route("/send-certificates", methods=["POST"])
    @login_required
    def send_certificates():
        """Create a background job to render and email all certificates."""
        from datetime import datetime, timedelta, timezone
        from models import Job
        from jobs import start_job

        draft = _get_preview_draft()
        if draft is None:
            return jsonify({"error": "No data to send"}), 400
        config = json.loads(draft.config_json)
        students = json.loads(draft.students_json)

        # Duplicate-send guard: if the user already has a job that's queued
        # or running and was created in the last 10 minutes, refuse to start
        # a second one. Protects against double-clicks and accidental
        # refreshes that would otherwise email every student twice.
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        in_flight = (
            Job.query
            .filter(Job.created_by == current_user.id)
            .filter(Job.status.in_(("queued", "running")))
            .filter(Job.created_at >= recent_cutoff)
            .first()
        )
        if in_flight is not None:
            return jsonify({
                "error": "A send is already in progress. Please wait for "
                         "it to complete.",
                "job_id": in_flight.id,
            }), 400

        job = Job(
            status="queued",
            total_students=len(students),
            config_json=json.dumps(config),
            students_json=json.dumps(students),
            created_by=current_user.id,
        )
        db.session.add(job)
        db.session.commit()

        start_job(app, job.id)

        return jsonify({"redirect": url_for("job_progress", job_id=job.id)})

    # --- Resend only missing students from a prior job ---

    @app.route("/jobs/<int:job_id>/resend-missing", methods=["POST"])
    @login_required
    def resend_missing(job_id):
        """Create a new job that re-sends only students who did NOT receive
        the certificate in a prior (typically failed) job. "Missing" =
        anyone from the original batch without a 'sent' SendHistory row,
        so this covers both explicitly failed sends and students who were
        never reached because the worker died mid-batch.

        Safe to call repeatedly: if the original batch fully succeeded,
        the set of missing students is empty and we return 400.
        """
        from datetime import datetime, timedelta, timezone
        from models import Job, SendHistory
        from jobs import start_job

        job = db.session.get(Job, job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        if job.created_by != current_user.id:
            return jsonify({"error": "Not your job"}), 403

        original_students = json.loads(job.students_json)
        sent_emails = {
            h.student_email for h in
            SendHistory.query.filter_by(job_id=job.id, status="sent").all()
        }
        missing = [s for s in original_students
                   if s.get("email") not in sent_emails]

        if not missing:
            return jsonify({
                "error": "No students to re-send — every student in this "
                         "batch already has a successful send on record.",
            }), 400

        # Same duplicate-send guard as /send-certificates. Applies here too:
        # if a resend is already running we shouldn't kick off another one.
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        in_flight = (
            Job.query
            .filter(Job.created_by == current_user.id)
            .filter(Job.status.in_(("queued", "running")))
            .filter(Job.created_at >= recent_cutoff)
            .first()
        )
        if in_flight is not None:
            return jsonify({
                "error": "A send is already in progress. Please wait for "
                         "it to complete.",
                "job_id": in_flight.id,
            }), 400

        new_job = Job(
            status="queued",
            total_students=len(missing),
            config_json=job.config_json,  # reuse the original snapshot
            students_json=json.dumps(missing),
            created_by=current_user.id,
        )
        db.session.add(new_job)
        db.session.commit()

        start_job(app, new_job.id)

        return jsonify({
            "redirect": url_for("job_progress", job_id=new_job.id),
            "missing_count": len(missing),
        })

    # --- Job progress page ---

    @app.route("/jobs/<int:job_id>")
    @login_required
    def job_progress(job_id):
        from models import Job
        job = db.session.get(Job, job_id)
        if not job:
            flash("Job not found.", "error")
            return redirect(url_for("dashboard"))
        config = json.loads(job.config_json)
        students = json.loads(job.students_json)
        return render_template("job.html", job=job, config=config, students=students)

    # --- Job status API (polled by frontend) ---

    @app.route("/jobs/<int:job_id>/status")
    @login_required
    def job_status(job_id):
        from models import Job, SendHistory
        job = db.session.get(Job, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        # Get per-student results
        history = SendHistory.query.filter_by(job_id=job_id).order_by(
            SendHistory.id).all()
        results = [{
            "name": h.student_name,
            "email": h.student_email,
            "status": h.status,
            "error": h.error_message,
        } for h in history]

        return jsonify({
            "status": job.status,
            "total": job.total_students,
            "processed": job.processed_count,
            "error": job.error_message,
            "results": results,
        })

    # --- Download all PDFs as zip ---

    @app.route("/download-all", methods=["POST"])
    @login_required
    def download_all():
        """Render all certificates and return as a zip file."""
        import asyncio
        import zipfile
        from renderer import CertificateRenderer

        draft = _get_preview_draft()
        if draft is None:
            return jsonify({"error": "No data"}), 400
        config = json.loads(draft.config_json)
        students = json.loads(draft.students_json)

        output_dir = ROOT / "output" / "download"
        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = ROOT / "output" / "certificates.zip"

        async def do_render():
            pdf_paths = []
            async with CertificateRenderer() as renderer:
                for student in students:
                    slug = "".join(c if c.isalnum() else "_"
                                   for c in student["name"]).strip("_")
                    pdf_path = output_dir / f"certificate_{slug}.pdf"
                    await renderer.render(
                        _build_render_data(student, config), pdf_path)
                    pdf_paths.append(pdf_path)
            return pdf_paths

        pdf_paths = asyncio.run(do_render())

        with zipfile.ZipFile(zip_path, "w") as zf:
            for p in pdf_paths:
                zf.write(p, p.name)

        from flask import send_file
        return send_file(str(zip_path), mimetype="application/zip",
                         download_name="certificates.zip", as_attachment=True)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
