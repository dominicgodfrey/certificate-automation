"""ThinkNeuro Certificate Automation — Web Application."""
import hmac
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)

from store import preset_store, job_store, preview_drafts
from template_registry import TEMPLATES, DEFAULT_TEMPLATE, template_file

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

ADMIN_USER_ID = "admin"


class AdminUser(UserMixin):
    """The single operator account, defined by env vars — no database.

    Username comes from DEFAULT_ADMIN_USER (default 'admin'); the
    password is DEFAULT_ADMIN_PASSWORD and must be set for login to work.
    """
    id = ADMIN_USER_ID

    @property
    def username(self):
        return os.environ.get("DEFAULT_ADMIN_USER", "admin")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "web_templates"),
        static_folder=str(ROOT / "static"),
    )

    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

    # No database: presets live in presets.json (+ a local overlay),
    # jobs and previews are in-memory (single Gunicorn worker), and the
    # durable record of every batch is its results CSV (download/email).
    print("Storage: presets.json + in-memory jobs (no database)")
    if not os.environ.get("DEFAULT_ADMIN_PASSWORD"):
        print("WARNING: DEFAULT_ADMIN_PASSWORD is not set — login disabled.")

    # Loud-but-non-fatal email-provider credential smoke test. Catches
    # a revoked / rate-limited SendGrid API key before an operator
    # clicks Send and watches 400 students fail in a row.
    try:
        from emailer import EmailSender
        ok, msg = EmailSender.smoke_test()
        print(("Email OK: " if ok else "Email WARNING: ") + msg)
    except Exception as e:
        print(f"Email smoke test crashed (non-fatal): {e}")

    # --- Login manager ---
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = ""  # suppress the default "please log in" flash

    @login_manager.user_loader
    def load_user(user_id):
        if user_id == ADMIN_USER_ID:
            return AdminUser()
        return None

    # --- Helpers ---

    def _get_preview_draft():
        """Return the current user's active preview draft, or None."""
        return preview_drafts.get(current_user.id)

    # --- Routes ---

    @app.route("/healthz", methods=["GET"])
    def healthz():
        """Liveness probe + keep-alive target.

        The background job thread pings this from within the same instance
        during long batches so the host platform sees continuous HTTP
        traffic and doesn't spin the web instance down mid-send. No auth,
        no storage hit — must stay cheap.
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

            expected_user = os.environ.get("DEFAULT_ADMIN_USER", "admin")
            expected_pass = os.environ.get("DEFAULT_ADMIN_PASSWORD", "")
            # compare_digest on both fields to avoid timing side-channels;
            # an empty configured password always fails.
            if expected_pass \
                    and hmac.compare_digest(username, expected_user) \
                    and hmac.compare_digest(password, expected_pass):
                login_user(AdminUser())
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
        presets = preset_store.list()
        # Shape presets for the template so the frontend can filter by
        # template_id client-side without an extra request.
        presets_view = [
            {"id": p["id"], "name": p["name"], "template_id": p["template_id"]}
            for p in presets
        ]
        # Load last-used preset from session, or default to empty
        active_preset_id = session.get("active_preset_id")
        active_config = None
        active_template_id = session.get("active_template_id", DEFAULT_TEMPLATE)
        if active_preset_id:
            preset = preset_store.get(active_preset_id)
            if preset:
                active_config = preset["config"]
                active_template_id = preset["template_id"]

        # Surface any of THIS user's failed jobs that still have students
        # missing a successful send. Lets the operator finish an
        # interrupted batch from the dashboard instead of having to
        # remember the URL of the failed job page.
        unfinished = job_store.unfinished(current_user.id)

        return render_template("dashboard.html",
                               presets=presets_view,
                               active_preset_id=active_preset_id,
                               active_config=active_config,
                               active_template_id=active_template_id,
                               templates_meta=TEMPLATES,
                               default_template_id=DEFAULT_TEMPLATE,
                               unfinished_jobs=unfinished)

    # --- Preset API routes ---

    @app.route("/presets/load/<int:preset_id>", methods=["GET"])
    @login_required
    def load_preset(preset_id):
        preset = preset_store.get(preset_id)
        if not preset:
            return jsonify({"error": "Preset not found"}), 404
        session["active_preset_id"] = preset_id
        session["active_template_id"] = preset["template_id"]
        return jsonify({
            "config": preset["config"],
            "name": preset["name"],
            "template_id": preset["template_id"],
        })

    @app.route("/presets/save", methods=["POST"])
    @login_required
    def save_preset():
        data = request.get_json()
        name = data.get("name", "").strip()
        config = data.get("config")
        # Template id can come top-level or embedded in config; prefer top-level.
        template_id = (data.get("template_id")
                       or (config or {}).get("template_id")
                       or DEFAULT_TEMPLATE)
        if template_id not in TEMPLATES:
            template_id = DEFAULT_TEMPLATE

        if not name or not config:
            return jsonify({"error": "Name and config are required"}), 400

        existing = preset_store.find_by_name(name)
        # Don't let a save reassign a preset to a different template —
        # presets are template-scoped, so a same-name preset under a
        # different template is a separate entry.
        if existing and existing["template_id"] != template_id:
            return jsonify({
                "error": (f"A preset named '{name}' already exists for "
                          f"the '{existing['template_id']}' template. "
                          "Choose a different name.")
            }), 400

        preset, created = preset_store.save(name, config, template_id)
        session["active_preset_id"] = preset["id"]
        session["active_template_id"] = preset["template_id"]
        verb = "Saved" if created else "Updated"
        return jsonify({"message": f"{verb} '{name}'", "id": preset["id"]})

    @app.route("/presets/delete/<int:preset_id>", methods=["DELETE"])
    @login_required
    def delete_preset(preset_id):
        name = preset_store.delete(preset_id)
        if name is None:
            return jsonify({"error": "Preset not found"}), 404
        if session.get("active_preset_id") == preset_id:
            session.pop("active_preset_id", None)
        return jsonify({"message": f"Deleted '{name}'"})

    @app.route("/presets/export/<int:preset_id>", methods=["GET"])
    @login_required
    def export_preset(preset_id):
        """Download a preset as JSON, ready to paste into presets.json.

        Presets saved through the UI live on the instance's ephemeral
        disk and are lost on redeploy; adding the exported entry to the
        repo's presets.json makes it permanent.
        """
        preset = preset_store.get(preset_id)
        if not preset:
            return jsonify({"error": "Preset not found"}), 404
        slug = "".join(c if c.isalnum() else "_" for c in preset["name"]).strip("_")
        from flask import Response
        return Response(
            json.dumps(preset, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition":
                     f"attachment; filename=preset_{slug}.json"},
        )

    # --- CSV upload ---

    @app.route("/upload-csv", methods=["POST"])
    @login_required
    def upload_csv():
        """Parse an uploaded CSV/XLSX and return headers + rows as JSON.

        Special case: a results CSV from a previous batch (headers
        name/email/status/error) is the retry path after a server
        restart — rows already marked 'sent' are dropped so the operator
        can re-send to just the students who still need a certificate.
        """
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

        note = None
        from jobs import RESULTS_CSV_HEADERS
        if list(df.columns) == RESULTS_CSV_HEADERS:
            before = len(df)
            df = df[df["status"].str.strip().str.lower() != "sent"]
            excluded = before - len(df)
            note = (f"Results CSV detected — excluded {excluded} already-sent "
                    f"student{'s' if excluded != 1 else ''}; "
                    f"{len(df)} left to send.")
            if len(df) == 0:
                return jsonify({"error":
                                "Results CSV detected, but every student in it "
                                "was already sent — nothing to re-send."}), 400

        headers = list(df.columns)
        rows = df.astype(str).values.tolist()

        return jsonify({"headers": headers, "rows": rows, "note": note})

    # --- Generate preview ---

    @app.route("/generate-preview", methods=["POST"])
    @login_required
    def generate_preview():
        """Store certificate config + student list as the user's draft.

        Kept server-side in memory (not in the session cookie) so that
        large batches — up to 1000+ students — don't exceed the ~4KB
        signed cookie limit and silently fail.
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

        # Replaces any prior draft for this user, so abandoned previews
        # don't accumulate.
        preview_drafts[current_user.id] = {
            "config": config,
            "students": students,
        }

        return jsonify({"redirect": url_for("preview")})

    # --- Preview page ---

    @app.route("/preview")
    @login_required
    def preview():
        draft = _get_preview_draft()
        if draft is None:
            flash("No data to preview. Please fill out the form first.", "error")
            return redirect(url_for("dashboard"))
        return render_template("preview.html", config=draft["config"],
                               students=draft["students"])

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
        config = draft["config"]
        students = draft["students"]
        if student_index < 0 or student_index >= len(students):
            return "Invalid student index", 400

        student = students[student_index]
        render_data = _build_render_data(student, config)
        tpl_file = template_file(config.get("template_id", DEFAULT_TEMPLATE))

        output_dir = ROOT / "output" / "preview"
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / f"preview_{student_index}.pdf"

        async def do_render():
            async with CertificateRenderer(template_name=tpl_file) as renderer:
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
        config = draft["config"]
        students = draft["students"]
        if student_index < 0 or student_index >= len(students):
            return "Invalid student index", 400

        student = students[student_index]
        render_data = _build_render_data(student, config)
        tpl_file = template_file(config.get("template_id", DEFAULT_TEMPLATE))

        output_dir = ROOT / "output" / "preview"
        output_dir.mkdir(parents=True, exist_ok=True)
        png_path = output_dir / f"preview_{student_index}.png"

        async def do_render():
            env = Environment(
                loader=FileSystemLoader(str(TEMPLATES)),
                autoescape=True,
            )
            template = env.get_template(tpl_file)
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
        from jobs import start_job

        draft = _get_preview_draft()
        if draft is None:
            return jsonify({"error": "No data to send"}), 400

        # Duplicate-send guard: if the user already has a job that's queued
        # or running and was created in the last 10 minutes, refuse to start
        # a second one. Protects against double-clicks and accidental
        # refreshes that would otherwise email every student twice.
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        in_flight = job_store.in_flight(current_user.id, recent_cutoff)
        if in_flight is not None:
            return jsonify({
                "error": "A send is already in progress. Please wait for "
                         "it to complete.",
                "job_id": in_flight["id"],
            }), 400

        job = job_store.create(
            config=draft["config"],
            students=draft["students"],
            created_by=current_user.id,
        )

        start_job(job["id"])

        return jsonify({"redirect": url_for("job_progress", job_id=job["id"])})

    # --- Resend only missing students from a prior job ---

    @app.route("/jobs/<int:job_id>/resend-missing", methods=["POST"])
    @login_required
    def resend_missing(job_id):
        """Create a new job that re-sends only students who did NOT receive
        the certificate in a prior (typically failed) job. "Missing" =
        anyone from the original batch without a 'sent' result, so this
        covers both explicitly failed sends and students who were never
        reached because the worker died mid-batch.

        Safe to call repeatedly: if the original batch fully succeeded,
        the set of missing students is empty and we return 400.
        """
        from datetime import datetime, timedelta, timezone
        from jobs import start_job

        job = job_store.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        if job["created_by"] != current_user.id:
            return jsonify({"error": "Not your job"}), 403

        sent_emails = job_store.sent_emails(job_id)
        missing = [s for s in job["students"]
                   if s.get("email") not in sent_emails]

        if not missing:
            return jsonify({
                "error": "No students to re-send — every student in this "
                         "batch already has a successful send on record.",
            }), 400

        # Same duplicate-send guard as /send-certificates. Applies here too:
        # if a resend is already running we shouldn't kick off another one.
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        in_flight = job_store.in_flight(current_user.id, recent_cutoff)
        if in_flight is not None:
            return jsonify({
                "error": "A send is already in progress. Please wait for "
                         "it to complete.",
                "job_id": in_flight["id"],
            }), 400

        new_job = job_store.create(
            config=job["config"],  # reuse the original snapshot
            students=missing,
            created_by=current_user.id,
        )

        start_job(new_job["id"])

        return jsonify({
            "redirect": url_for("job_progress", job_id=new_job["id"]),
            "missing_count": len(missing),
        })

    # --- Job progress page ---

    @app.route("/jobs/<int:job_id>")
    @login_required
    def job_progress(job_id):
        job = job_store.get(job_id)
        if not job:
            flash("Job not found — the server may have restarted. If a send "
                  "was interrupted, re-upload its results CSV (emailed to "
                  "the operator inbox) to resume without duplicates.", "error")
            return redirect(url_for("dashboard"))
        return render_template("job.html", job=job, config=job["config"],
                               students=job["students"])

    # --- Job status API (polled by frontend) ---

    @app.route("/jobs/<int:job_id>/status")
    @login_required
    def job_status(job_id):
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        return jsonify({
            "status": job["status"],
            "total": job["total_students"],
            "processed": job["processed_count"],
            "error": job["error_message"],
            "results": list(job["results"]),
        })

    # --- Job results CSV (the durable, operator-held send record) ---

    @app.route("/jobs/<int:job_id>/results.csv")
    @login_required
    def job_results_csv(job_id):
        from jobs import results_csv_text
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        from flask import Response
        return Response(
            results_csv_text(job),
            mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=job_{job_id}_results.csv"},
        )

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
        config = draft["config"]
        students = draft["students"]

        output_dir = ROOT / "output" / "download"
        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = ROOT / "output" / "certificates.zip"
        tpl_file = template_file(config.get("template_id", DEFAULT_TEMPLATE))

        async def do_render():
            pdf_paths = []
            async with CertificateRenderer(template_name=tpl_file) as renderer:
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
