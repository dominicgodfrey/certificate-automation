"""SMTP email sender with PDF attachment support.

Isolated so you can swap SMTP for SendGrid / AWS SES / etc. without
touching the rest of the pipeline.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


class EmailError(Exception):
    pass


class EmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        sender_name: str,
        sender_email: str,
        dry_run: bool = False,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender_name = sender_name
        self.sender_email = sender_email
        self.dry_run = dry_run

    @classmethod
    def from_env(cls) -> "EmailSender":
        required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                    "SENDER_NAME", "SENDER_EMAIL"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise EmailError(f"Missing env vars: {missing}. Copy .env.example to .env.")
        return cls(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ["SMTP_PORT"]),
            user=os.environ["SMTP_USER"],
            password=os.environ["SMTP_PASSWORD"],
            sender_name=os.environ["SENDER_NAME"],
            sender_email=os.environ["SENDER_EMAIL"],
            dry_run=os.environ.get("SEND_EMAILS", "false").lower() != "true",
        )

    def send(self, to: str, subject: str, body: str, attachment_path: Path) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = to
        msg.set_content(body, charset="utf-8")

        attachment_path = Path(attachment_path)
        if not attachment_path.exists():
            raise EmailError(f"Attachment not found: {attachment_path}")
        with open(attachment_path, "rb") as f:
            data = f.read()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename=attachment_path.name,
        )

        if self.dry_run:
            print(f"  [DRY RUN] would send to {to}, subject='{subject}', "
                  f"attachment={attachment_path.name} ({len(data)} bytes)")
            return

        ctx = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port) as server:
            server.starttls(context=ctx)
            server.login(self.user, self.password)
            server.send_message(msg)
