"""SMTP email sender with PDF attachment support.

Isolated so you can swap SMTP for SendGrid / AWS SES / etc. without
touching the rest of the pipeline.

The sender holds a persistent SMTP connection across calls when used
as a context manager; this avoids re-authenticating on every message
and stays well under typical SMTP login-rate-limit thresholds during
bulk sends. It reconnects automatically on dropped connections and
recycles the connection every RECONNECT_EVERY messages as a safety
valve against server-side idle timeouts.
"""
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path


RECONNECT_EVERY = 50  # proactively recycle the SMTP connection every N sends


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
        self._server: smtplib.SMTP | None = None
        self._send_count = 0

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

    # --- connection management -------------------------------------------------

    def _open(self) -> None:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP(self.host, self.port, timeout=30)
        server.starttls(context=ctx)
        server.login(self.user, self.password)
        self._server = server
        self._send_count = 0

    def _close(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:
                pass
            self._server = None

    def __enter__(self) -> "EmailSender":
        if not self.dry_run:
            self._open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._close()
        return False

    # --- send ------------------------------------------------------------------

    def send(self, to: str, subject: str, body: str, attachment_path: Path) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = to
        # Reply-To so a recipient hitting reply lands on a monitored inbox,
        # not the SMTP user (which may be different on a relayed setup).
        msg["Reply-To"] = self.sender_email
        # List-Unsubscribe is treated as a strong "this is a legitimate
        # bulk sender" signal by Gmail/Outlook/Yahoo. Mailto form works
        # without needing a public unsubscribe URL.
        msg["List-Unsubscribe"] = (
            f"<mailto:{self.sender_email}?subject=Unsubscribe>"
        )
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

        # Proactively recycle the connection every N messages so we don't
        # get silently dropped by a server-side idle timeout.
        if self._send_count >= RECONNECT_EVERY:
            self._close()

        if self._server is None:
            self._open()

        # Try once; on a dropped connection or transient SMTP error, reconnect
        # and retry exactly once before giving up.
        try:
            self._server.send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                ConnectionError, BrokenPipeError, OSError) as e:
            self._close()
            time.sleep(2)
            try:
                self._open()
                self._server.send_message(msg)
            except Exception as retry_err:
                raise EmailError(
                    f"SMTP send failed after reconnect: {retry_err} "
                    f"(original: {e})"
                ) from retry_err
        except smtplib.SMTPException as e:
            # Transient SMTP-level error (e.g. 4xx). Reconnect and retry once.
            self._close()
            time.sleep(2)
            try:
                self._open()
                self._server.send_message(msg)
            except Exception as retry_err:
                raise EmailError(
                    f"SMTP send failed after retry: {retry_err} "
                    f"(original: {e})"
                ) from retry_err

        self._send_count += 1
