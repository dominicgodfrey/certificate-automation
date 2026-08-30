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
import base64
import itertools
import json
import os
import threading
import urllib.error
import urllib.request
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


def github_sync_enabled() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN"))


def _sync_presets_to_github(presets: list[dict]) -> str | None:
    """Commit the merged preset list to presets.json in the GitHub repo.

    This is what makes UI-created presets durable without touching code:
    the running instance serves them from the local overlay immediately,
    and this commit ensures the next deploy ships them too. The commit
    message carries "[skip render]" so Render does NOT auto-deploy —
    a redeploy here would kill any batch in progress for no benefit.

    No-op when GITHUB_TOKEN is unset (the manual Export workflow applies).
    Returns None on success, or a short warning string on failure —
    never raises, because the local save has already succeeded.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    repo = os.environ.get("GITHUB_REPO", "dominicgodfrey/certificate-automation")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://api.github.com/repos/{repo}/contents/presets.json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "cert-app-preset-sync",
    }
    body = json.dumps(presets, indent=2) + "\n"
    try:
        # Current file sha is required to update; 404 means create.
        sha = None
        try:
            req = urllib.request.Request(f"{url}?ref={branch}", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                sha = json.loads(resp.read())["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        payload = {
            "message": "[skip render] Update presets from web UI",
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT")
        with urllib.request.urlopen(req, timeout=15):
            pass
        print(f"Preset sync: committed presets.json to {repo}@{branch}")
        return None
    except Exception as e:
        print(f"Warning: GitHub preset sync failed (preset saved locally): {e}")
        return ("saved on this instance only — GitHub sync failed, so it "
                "won't survive a redeploy (check GITHUB_TOKEN)")


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

    def save(self, name: str, config: dict, template_id: str) -> tuple[dict, bool, str | None]:
        """Create or update a preset by name.

        Returns (preset, created, sync_warning). sync_warning is None
        unless the GitHub durability commit failed (local save still ok).
        """
        existing = self.find_by_name(name)
        with self._lock:
            local = self._read_local()
            if existing:
                created = False
                preset = {**existing, "config": config}
                # Replace any prior overlay entry for this id.
                local["presets"] = [p for p in local["presets"]
                                    if p["id"] != existing["id"]] + [preset]
            else:
                created = True
                all_ids = [p["id"] for p in self._read_repo()] + \
                          [p["id"] for p in local["presets"]] + \
                          local["deleted_ids"]
                new_id = max(all_ids + [LOCAL_ID_START - 1]) + 1
                preset = {"id": new_id, "name": name,
                          "template_id": template_id, "config": config}
                local["presets"].append(preset)
            self._write_local(local)
        warning = _sync_presets_to_github(self.list())
        return preset, created, warning

    def delete(self, preset_id: int) -> tuple[str | None, str | None]:
        """Hide/remove a preset. Returns (name, sync_warning); name is
        None if the preset wasn't found.

        Without GitHub sync, a repo-backed preset reappears on the next
        deploy unless also removed from presets.json by hand; with sync,
        the removal is committed to the repo too.
        """
        preset = self.get(preset_id)
        if preset is None:
            return None, None
        with self._lock:
            local = self._read_local()
            local["presets"] = [p for p in local["presets"]
                                if p["id"] != preset_id]
            if preset_id not in local["deleted_ids"]:
                local["deleted_ids"].append(preset_id)
            self._write_local(local)
        warning = _sync_presets_to_github(self.list())
        return preset["name"], warning


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
