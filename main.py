"""Main pipeline: spreadsheet → render PDFs → email each student.

Usage:
    python main.py                  # uses config.yaml
    python main.py --config foo.yaml
"""
import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml
from dotenv import load_dotenv

from spreadsheet import Student, load_students, SpreadsheetError
from renderer import CertificateRenderer
from emailer import EmailSender, EmailError

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output" / "certificates"


@dataclass
class Result:
    student: Student
    pdf_path: Path | None = None
    sent: bool = False
    error: str | None = None


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")


def build_render_data(student: Student, cert_cfg: dict) -> dict:
    return {
        "student_name": student.name,
        "date": cert_cfg["date"],
        "program_title": cert_cfg["program_title"],
        "program_description": cert_cfg["program_description"],
        "hours": cert_cfg["hours"],
        "footer": cert_cfg["footer"],
        "signatories": cert_cfg["signatories"],
    }


async def run(config_path: Path) -> list[Result]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Load students
    sheet_cfg = cfg["spreadsheet"]
    try:
        students = load_students(
            ROOT / sheet_cfg["path"],
            sheet_cfg["name_column"],
            sheet_cfg["email_column"],
        )
    except SpreadsheetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Loaded {len(students)} students from {sheet_cfg['path']}")

    # 2. Set up email sender (dry-run by default — see SEND_EMAILS in .env)
    try:
        sender = EmailSender.from_env()
    except EmailError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if sender.dry_run:
        print("DRY RUN MODE — emails will be printed, not sent.")
        print("Set SEND_EMAILS=true in .env to actually send.")

    # 3. Render certificates and send emails
    results: list[Result] = []
    cert_cfg = cfg["certificate"]
    email_cfg = cfg["email"]

    async with CertificateRenderer() as renderer:
        for student in students:
            result = Result(student=student)
            try:
                pdf_path = OUTPUT_DIR / f"certificate_{slugify(student.name)}.pdf"
                await renderer.render(build_render_data(student, cert_cfg), pdf_path)
                result.pdf_path = pdf_path
                print(f"  Rendered {pdf_path.name}")

                # safe_substitute ignores missing/malformed keys instead of raising
                body = Template(email_cfg["body"]).safe_substitute(
                    first_name=student.first_name
                )
                sender.send(
                    to=student.email,
                    subject=email_cfg["subject"],
                    body=body,
                    attachment_path=pdf_path,
                )
                result.sent = True

                # Rate limit: avoid triggering spam filters on bulk sends
                if not sender.dry_run:
                    time.sleep(2)
            except Exception as e:
                result.error = str(e)
                print(f"  FAILED for {student.name}: {e}", file=sys.stderr)
            results.append(result)

    # 4. Summary
    succeeded = sum(1 for r in results if r.sent)
    failed = sum(1 for r in results if r.error)
    print(f"\nDone. {succeeded} succeeded, {failed} failed, "
          f"{len(results)} total.")
    if failed:
        print("Failures:")
        for r in results:
            if r.error:
                print(f"  {r.student.name} <{r.student.email}>: {r.error}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
