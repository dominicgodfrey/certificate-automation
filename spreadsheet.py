"""Read and validate the student spreadsheet.

Isolated so you can swap CSV/XLSX/Google Sheets without touching anything else.
"""
from dataclasses import dataclass
from pathlib import Path
import pandas as pd


@dataclass
class Student:
    name: str
    email: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else ""


class SpreadsheetError(Exception):
    pass


def load_students(path: str | Path, name_col: str, email_col: str) -> list[Student]:
    """Load students from a CSV or XLSX file.

    Validates that required columns exist and that no rows have missing data.
    Raises SpreadsheetError with a clear message on any issue.
    """
    path = Path(path)
    if not path.exists():
        raise SpreadsheetError(f"Spreadsheet not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        raise SpreadsheetError(f"Unsupported file type: {suffix}")

    missing_cols = [c for c in (name_col, email_col) if c not in df.columns]
    if missing_cols:
        raise SpreadsheetError(
            f"Missing required column(s) {missing_cols}. "
            f"Found columns: {list(df.columns)}"
        )

    students: list[Student] = []
    issues: list[str] = []
    for i, row in df.iterrows():
        name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
        email = str(row[email_col]).strip() if pd.notna(row[email_col]) else ""
        if not name or not email:
            issues.append(f"Row {i + 2}: missing name or email")
            continue
        if "@" not in email:
            issues.append(f"Row {i + 2}: invalid email '{email}'")
            continue
        students.append(Student(name=name, email=email))

    if issues:
        raise SpreadsheetError("Spreadsheet validation failed:\n  " + "\n  ".join(issues))

    if not students:
        raise SpreadsheetError("No valid student rows found.")

    return students
