"""Database models for the certificate web application."""
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<User {self.username}>"


class Preset(db.Model):
    __tablename__ = "presets"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    config_json = db.Column(db.Text, nullable=False)  # JSON blob of certificate settings
    # Which certificate template this preset targets. server_default lets
    # existing rows in SQLite/Postgres backfill cleanly on first create_all().
    template_id = db.Column(db.String(50), nullable=False,
                            server_default="completion", default="completion")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Preset {self.name}>"


class SendHistory(db.Model):
    __tablename__ = "send_history"
    id = db.Column(db.Integer, primary_key=True)
    student_name = db.Column(db.String(200), nullable=False)
    student_email = db.Column(db.String(200), nullable=False)
    preset_name = db.Column(db.String(200))
    status = db.Column(db.String(20), nullable=False)  # "sent" or "failed"
    error_message = db.Column(db.Text)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=True)
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<SendHistory {self.student_name} - {self.status}>"


class PreviewDraft(db.Model):
    """Server-side storage of a preview's config + student list.

    Replaces the old session-cookie approach, which silently failed for
    batches of ~500+ students (4KB signed-cookie ceiling).
    """
    __tablename__ = "preview_drafts"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    config_json = db.Column(db.Text, nullable=False)
    students_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<PreviewDraft {self.id} user={self.user_id}>"


class Job(db.Model):
    __tablename__ = "jobs"
    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(20), nullable=False, default="queued")
    # queued, running, complete, failed
    total_students = db.Column(db.Integer, nullable=False, default=0)
    processed_count = db.Column(db.Integer, nullable=False, default=0)
    config_json = db.Column(db.Text, nullable=False)
    students_json = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    history = db.relationship("SendHistory", backref="job", lazy=True)

    def __repr__(self):
        return f"<Job {self.id} - {self.status} ({self.processed_count}/{self.total_students})>"
