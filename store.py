"""In-memory + file-backed storage. Replaces the old database models.

Safe because Gunicorn runs exactly 1 worker (see Dockerfile), so one
process owns all state. Student PII intentionally never persists on the
server — the durable send record is the results CSV that leaves the
instance. Presets live in repo-committed presets.json plus an ephemeral
local overlay (data/presets_local.json) for UI edits; GitHub sync or the
Export button makes UI edits permanent.
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
    """Commit the merged preset list to presets.json in the GitHub repo,
    making UI-created presets survive redeploys. "[skip render]" in the
    commit message stops Render from auto-deploying (a redeploy could
    kill a batch in progress; the overlay already serves the preset).

    No-op without GITHUB_TOKEN. Returns None on success or a short
    warning on failure — never raises; the local save already landed.
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
    """Merged view of repo presets + local overlay: same-id overlay
    entries shadow repo entries (edit), `deleted_ids` hide them (delete)."""

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
        None if not found. Without GitHub sync a repo-backed preset
        reappears on the next deploy unless removed from presets.json."""
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
    """In-memory send jobs. Lost on restart by design — the durable
    record of a batch is its results CSV."""

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

# Preview drafts keyed by user id; replaced wholesale on each new preview.
preview_drafts: dict[str, dict] = {}
