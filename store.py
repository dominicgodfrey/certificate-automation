"""In-memory + file-backed storage. Replaces the old Postgres/SQLite models.

Design constraints that make this safe:
- Gunicorn runs exactly 1 worker (see Dockerfile), so a single process
  owns all state; no cross-process coordination is needed.
- Durability for send results is provided by results CSVs that leave the
  instance (download + emailed reports), not by server-side storage —
  student PII intentionally never persists on the server.

Presets are the one thing with file backing:
- presets.json (committed to the repo) ships with every deploy and is
  the durable home for presets worth keeping.
- data/presets_local.json is an ephemeral overlay for presets created
  or edited through the UI. It survives worker restarts on the same
  instance but is lost on redeploy — promote a preset to presets.json
  (via the Export button) to make it permanent.
"""
import itertools
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

REPO_PRESETS_FILE = ROOT / "presets.json"
LOCAL_PRESETS_FILE = ROOT / "data" / "presets_local.json"
# Runtime-created presets get ids from 1001 up so they can never collide
# with hand-authored ids in the repo file.
LOCAL_ID_START = 1001


def _utcnow():
    return datetime.now(timezone.utc)


class PresetStore:
    """Merged view of repo presets + local overlay.

    Overlay semantics: a local preset with the same id as a repo preset
    shadows it (edit); ids in `deleted_ids` are hidden (delete). Both
    only last until the next deploy for repo-backed presets.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # -- file IO ----------------------------------------------------------

    def _read_repo(self) -> list[dict]:
        try:
            return json.loads(REPO_PRESETS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as e:
            print(f"Warning: could not parse {REPO_PRESETS_FILE.name}: {e}")
            return []

    def _read_local(self) -> dict:
        try:
            data = json.loads(LOCAL_PRESETS_FILE.read_text(encoding="utf-8"))
            return {"presets": data.get("presets", []),
                    "deleted_ids": data.get("deleted_ids", [])}
        except FileNotFoundError:
            return {"presets": [], "deleted_ids": []}
        except Exception as e:
            print(f"Warning: could not parse {LOCAL_PRESETS_FILE.name}: {e}")
            return {"presets": [], "deleted_ids": []}

    def _write_local(self, data: dict) -> None:
        LOCAL_PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_PRESETS_FILE.write_text(
            json.dumps(data, indent=2), encoding="utf-8")

    # -- queries ----------------------------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            local = self._read_local()
            merged = {p["id"]: p for p in self._read_repo()}
            merged.update({p["id"]: p for p in local["presets"]})
            for pid in local["deleted_ids"]:
                merged.pop(pid, None)
            return sorted(merged.values(), key=lambda p: p["name"].lower())

    def get(self, preset_id: int) -> dict | None:
        return next((p for p in self.list() if p["id"] == preset_id), None)

    def find_by_name(self, name: str) -> dict | None:
        return next((p for p in self.list() if p["name"] == name), None)

    # -- mutations (write to the local overlay only) ----------------------

    def save(self, name: str, config: dict, template_id: str) -> tuple[dict, bool]:
        """Create or update a preset by name. Returns (preset, created)."""
        existing = self.find_by_name(name)
        with self._lock:
            local = self._read_local()
            if existing:
                preset = {**existing, "config": config}
                # Replace any prior overlay entry for this id.
                local["presets"] = [p for p in local["presets"]
                                    if p["id"] != existing["id"]] + [preset]
                self._write_local(local)
                return preset, False
            all_ids = [p["id"] for p in self._read_repo()] + \
                      [p["id"] for p in local["presets"]] + \
                      local["deleted_ids"]
            new_id = max(all_ids + [LOCAL_ID_START - 1]) + 1
            preset = {"id": new_id, "name": name,
                      "template_id": template_id, "config": config}
            local["presets"].append(preset)
            self._write_local(local)
            return preset, True

    def delete(self, preset_id: int) -> str | None:
        """Hide/remove a preset. Returns its name, or None if not found.

        A repo-backed preset reappears on the next deploy unless it is
        also removed from presets.json.
        """
        preset = self.get(preset_id)
        if preset is None:
            return None
        with self._lock:
            local = self._read_local()
            local["presets"] = [p for p in local["presets"]
                                if p["id"] != preset_id]
            if preset_id not in local["deleted_ids"]:
                local["deleted_ids"].append(preset_id)
            self._write_local(local)
        return preset["name"]


class JobStore:
    """In-memory send jobs. Lost on restart — by design; the durable
    record of a batch is its results CSV (downloaded or emailed)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[int, dict] = {}
        self._ids = itertools.count(1)

    def create(self, config: dict, students: list, created_by) -> dict:
        with self._lock:
            job = {
                "id": next(self._ids),
                "status": "queued",
                "total_students": len(students),
                "processed_count": 0,
                "config": config,
                "students": students,
                "created_by": created_by,
                "created_at": _utcnow(),
                "completed_at": None,
                "error_message": None,
                "results": [],  # [{name, email, status, error}]
            }
            self._jobs[job["id"]] = job
            return job

    def get(self, job_id: int) -> dict | None:
        return self._jobs.get(job_id)

    def add_result(self, job_id: int, result: dict) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["results"].append(result)
            job["processed_count"] = len(job["results"])

    def sent_emails(self, job_id: int) -> set[str]:
        job = self._jobs.get(job_id)
        if not job:
            return set()
        with self._lock:
            return {r["email"] for r in job["results"] if r["status"] == "sent"}

    def in_flight(self, user_id, cutoff) -> dict | None:
        """First queued/running job for this user newer than cutoff."""
        with self._lock:
            return next(
                (j for j in self._jobs.values()
                 if j["created_by"] == user_id
                 and j["status"] in ("queued", "running")
                 and j["created_at"] >= cutoff),
                None)

    def unfinished(self, user_id, limit: int = 5) -> list[dict]:
        """Failed jobs that still have students without a successful send."""
        with self._lock:
            failed = sorted(
                (j for j in self._jobs.values()
                 if j["created_by"] == user_id and j["status"] == "failed"),
                key=lambda j: j["created_at"], reverse=True)[:limit]
            out = []
            for j in failed:
                sent = sum(1 for r in j["results"] if r["status"] == "sent")
                missing = j["total_students"] - sent
                if missing > 0:
                    out.append({"id": j["id"], "total": j["total_students"],
                                "sent": sent, "missing": missing,
                                "created_at": j["created_at"]})
            return out


preset_store = PresetStore()
job_store = JobStore()

# Preview drafts, keyed by user id. Replaced wholesale on each new
# preview, so this never holds more than one batch per user.
preview_drafts: dict[str, dict] = {}
