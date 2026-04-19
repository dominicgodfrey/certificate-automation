"""SendGrid-based email sender with PDF attachment support.

Uses SendGrid's HTTP API (not SMTP) because:
- Purpose-built for transactional bulk sending; Gmail/Outlook recognize
  SendGrid IPs as legitimate senders.
- DKIM is handled at the provider level — already live for
  thinkneuro.org via `s1._domainkey` CNAME.
- No SMTP App Password dependency. API keys are app-scoped and do not
  get revoked by Workspace policy changes.
- No auth throttling / reconnect dance needed. Each send is a single
  stateless HTTPS POST.

Interface mirrors the old SMTP EmailSender so jobs.py works unchanged:
  with EmailSender.from_env() as sender:
      sender.send(to, subject, body, attachment_path)
The context manager is a no-op (kept for backward compatibility).
"""
import base64
import os
from pathlib import Path

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, Attachment, FileContent, FileName, FileType, Disposition,
    ReplyTo, Header,
)


class EmailError(Exception):
    pass


class EmailSender:
    def __init__(
        self,
        api_key: str,
        sender_name: str,
        sender_email: str,
        dry_run: bool = False,
    ):
        self.api_key = api_key
        self.sender_name = sender_name
        self.sender_email = sender_email
        self.dry_run = dry_run

    @classmethod
    def from_env(cls) -> "EmailSender":
        required = ["SENDGRID_API_KEY", "SENDER_NAME", "SENDER_EMAIL"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise EmailError(
                f"Missing env vars: {missing}. Copy .env.example to .env."
            )
        return cls(
            api_key=os.environ["SENDGRID_API_KEY"],
            sender_name=os.environ["SENDER_NAME"],
            sender_email=os.environ["SENDER_EMAIL"],
            dry_run=os.environ.get("SEND_EMAILS", "false").lower() != "true",
        )

    @classmethod
    def smoke_test(cls) -> tuple[bool, str]:
        """Validate SENDGRID_API_KEY without sending mail.

        Hits GET /v3/user/profile: 200 means the key is valid; anything
        else means it's revoked, rate-limited, or misconfigured.
        Never raises — returns (ok, message) so app startup can log and
        continue.
        """
        try:
            sender = cls.from_env()
        except EmailError as e:
            return False, f"SendGrid config invalid: {e}"

        if sender.dry_run:
            return True, "SEND_EMAILS=false; SendGrid API key not tested"

        try:
            sg = SendGridAPIClient(sender.api_key)
            response = sg.client.user.profile.get()
            if response.status_code == 200:
                return True, (
                    f"SendGrid API key valid (sender "
                    f"{sender.sender_email})"
                )
            return False, (
                f"SendGrid API key check returned "
                f"{response.status_code}. Rotate SENDGRID_API_KEY."
            )
        except Exception as e:
            return False, (
                f"SendGrid API key check FAILED — {e}. "
                "Rotate SENDGRID_API_KEY."
            )

    # --- context manager (no-op for API-based sends) ---------------------

    def __enter__(self) -> "EmailSender":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    # --- send ------------------------------------------------------------

    def send(self, to: str, subject: str, body: str, attachment_path: Path) -> None:
        attachment_path = Path(attachment_path)
        if not attachment_path.exists():
            raise EmailError(f"Attachment not found: {attachment_path}")
        with open(attachment_path, "rb") as f:
            data = f.read()

        if self.dry_run:
            print(f"  [DRY RUN] would send to {to}, subject='{subject}', "
                  f"attachment={attachment_path.name} ({len(data)} bytes)")
            return

        encoded = base64.b64encode(data).decode()

        message = Mail(
            from_email=(self.sender_email, self.sender_name),
            to_emails=to,
            subject=subject,
            plain_text_content=body,
        )
        # Replies should land back on the monitored sender inbox, not on
        # whatever SendGrid's bounce address decays to.
        message.reply_to = ReplyTo(self.sender_email)
        # List-Unsubscribe is a strong "legitimate bulk sender" signal to
        # Gmail / Outlook / Yahoo. Mailto form doesn't require a public
        # unsubscribe URL.
        message.header = Header(
            "List-Unsubscribe",
            f"<mailto:{self.sender_email}?subject=Unsubscribe>",
        )
        message.attachment = Attachment(
            FileContent(encoded),
            FileName(attachment_path.name),
            FileType("application/pdf"),
            Disposition("attachment"),
        )

        try:
            sg = SendGridAPIClient(self.api_key)
            response = sg.send(message)
        except Exception as e:
            raise EmailError(
                f"SendGrid send failed for {to}: {e}"
            ) from e

        if response.status_code not in (200, 202):
            raise EmailError(
                f"SendGrid returned unexpected status "
                f"{response.status_code} for {to}: {response.body}"
            )
