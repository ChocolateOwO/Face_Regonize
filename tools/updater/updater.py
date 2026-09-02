"""Reconize detached updater.

Runs as its own process, outside the backend, because it has to stop and
restart that backend. The backend copies this file to the runtime directory
(%LOCALAPPDATA%\\Reconize\\updater\\) and launches the copy — never this
tracked original, because `git checkout` rewrites files inside the repository
and would otherwise be able to modify the updater while it is running.

Contract with the backend:
  * config is a JSON file passed as argv[1]
  * progress is written to update_state.json after every stage; that file is
    also what tells a freshly restarted backend that maintenance mode is
    still in force
  * the repository is PUBLIC, so every git operation here runs anonymously —
    no token, no credential helper, no askpass. Anything token-shaped that
    does appear in output is redacted before it is logged.

Stage order and the rollback ordering below are load-bearing; see the notes
at rollback().
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CONFIG: dict = {}
RUNTIME_DIR = Path(".")
STATE_FILE = Path("update_state.json")
LOG_FILE = Path("update.log")
LOCK_FILE = Path("update.lock")

BACKEND_PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/health"
STATUS_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/update/status"


# ---------------------------------------------------------------------------
# Secret hygiene / logging
# ---------------------------------------------------------------------------

def redact(text) -> str:
    """The public-repo updater handles no credentials of its own, but git and
    the environment can still surface a token-shaped string (a user-configured
    optional API token, a credential helper's message). Scrub defensively —
    the log is a file an admin may well paste into a bug report."""
    out = str(text or "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        out = out.replace(token, "***")
    out = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "***", out)
    out = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "***", out)
    out = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***@", out)
    return out


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(sep=' ', timespec='seconds')}  {redact(message)}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------

def write_state(**fields) -> None:
    """Atomic: a torn state file would strand the app in maintenance mode."""
    state = read_state()
    state.update(fields)
    state["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    if "error" in state and state["error"]:
        state["error"] = redact(state["error"])
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"could not write state file: {e}")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stage(name: str, status: str = "updating") -> None:
    log(f"STAGE: {name}")
    write_state(stage=name, status=status)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900, env: dict | None = None) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd or CONFIG["repo_root"]),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or git_env(),
    )
    if result.returncode != 0:
        log(f"  exit {result.returncode}: {redact(result.stderr.strip())[:800]}")
    return result


def git_env() -> dict:
    """The repository is public, so git runs anonymously — no credential
    helper, no askpass, no token. The prompt suppressions are defensive: this
    runs headless and detached, so a remote that unexpectedly asks for
    credentials must fail fast rather than hang forever on an invisible
    prompt."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def git(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return run(["git", *args], timeout=timeout)


def npm(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    exe = shutil.which("npm") or "npm"
    # npm on Windows is a .cmd shim, which needs the shell resolution that
    # shutil.which already did for us; call the resolved path directly.
    return run([exe, *args], cwd=Path(CONFIG["repo_root"]) / "frontend", timeout=timeout)


# ---------------------------------------------------------------------------
# Backend process control
# ---------------------------------------------------------------------------

def port_open(port: int = BACKEND_PORT, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pids_on_port(port: int = BACKEND_PORT) -> list[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: set[int] = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(int(parts[-1]))
    return sorted(pids)


def stop_backend(timeout: int = 45) -> bool:
    """Stop it, then PROVE it stopped. Returning early here is what would
    corrupt a database restore later, so this waits for actual exit rather
    than assuming the kill worked."""
    pids = pids_on_port()
    if not pids:
        log("backend not running")
        return True
    for pid in pids:
        log(f"stopping backend pid {pid}")
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True, timeout=30)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open() and not pids_on_port():
            log("backend stopped")
            return True
        time.sleep(0.5)
    log("WARNING: backend still listening after stop timeout")
    return False


def start_backend() -> None:
    repo = Path(CONFIG["repo_root"])
    python = CONFIG["python"]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    # Overridable so the updater can be exercised end-to-end against a
    # throwaway clone rather than only against the real installation. The
    # default is the production command.
    cmd = CONFIG.get("backend_start_cmd") or [
        python,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(BACKEND_PORT),
    ]
    log("starting backend")
    subprocess.Popen(
        cmd,
        cwd=str(repo / "backend"),
        stdin=subprocess.DEVNULL,
        stdout=open(repo / "backend" / "server_out.log", "ab"),
        stderr=open(repo / "backend" / "server_err.log", "ab"),
        close_fds=True,
        creationflags=creationflags,
    )


def wait_for_health(timeout: int = 180) -> bool:
    """The model loads at startup, so first health can legitimately take a
    while. A migration failure shows up as this never succeeding."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2)
    return False


def reported_version() -> str:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{BACKEND_PORT}/api/update/version", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("version", "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Database backup / restore
# ---------------------------------------------------------------------------

def backup_database() -> Path:
    """Online, consistent snapshot via SQLite's own backup API — a plain file
    copy of a live database can capture a torn write. Verified before the
    update is allowed to touch anything."""
    src = Path(CONFIG["database_path"])
    target_dir = Path(CONFIG["backup_dir"]) / f"{datetime.now():%Y%m%d_%H%M%S}_v{CONFIG['from_version']}"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "app.db"

    source = sqlite3.connect(str(src))
    try:
        destination = sqlite3.connect(str(dest))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    verify_backup(src, dest)

    env_file = Path(CONFIG["repo_root"]) / ".env"
    if env_file.exists():
        shutil.copy2(env_file, target_dir / ".env")
    log(f"database backed up and verified -> {dest}")
    return dest


COUNT_TABLES = ["person", "attendance", "consentrecord", "facedetection"]


def _counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        out: dict[str, int] = {}
        for table in COUNT_TABLES:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if cur.fetchone():
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        conn.close()


def verify_backup(src: Path, dest: Path) -> None:
    conn = sqlite3.connect(str(dest))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"backup failed integrity_check: {result}")
    finally:
        conn.close()

    src_counts, dest_counts = _counts(src), _counts(dest)
    if src_counts != dest_counts:
        raise RuntimeError(f"backup row counts differ: source={src_counts} backup={dest_counts}")
    log(f"backup verified: integrity ok, rows {dest_counts}")


def database_unheld(timeout: int = 60) -> bool:
    """Confirm nothing still holds the database open before replacing it.

    On Windows an open handle makes the replace fail outright; worse, a
    partially-written restore over a live database is corruption. So this
    waits for the port to be closed AND for an exclusive lock to be
    obtainable, and reports honestly if it never happens."""
    db = Path(CONFIG["database_path"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open() and not pids_on_port():
            try:
                conn = sqlite3.connect(str(db), timeout=2)
                try:
                    conn.execute("BEGIN EXCLUSIVE")
                    conn.execute("ROLLBACK")
                    return True
                finally:
                    conn.close()
            except sqlite3.Error as e:
                log(f"database still locked: {e}")
        time.sleep(1)
    return False


def restore_database(backup_path: Path) -> bool:
    """Only ever called after stop_backend() and database_unheld() have both
    succeeded — see rollback()."""
    db = Path(CONFIG["database_path"])
    try:
        for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if sidecar.exists():
                sidecar.unlink()
                log(f"removed {sidecar.name}")
        shutil.copy2(backup_path, db)
        conn = sqlite3.connect(str(db))
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if not check or check[0] != "ok":
            log(f"RESTORED DATABASE FAILED INTEGRITY CHECK: {check}")
            return False
        log("database restored and verified")
        return True
    except OSError as e:
        log(f"database restore failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Update stages
# ---------------------------------------------------------------------------

def manifest_changed(previous_sha: str, target_sha: str, path: str) -> bool:
    result = git("diff", "--name-only", previous_sha, target_sha, "--", path)
    return bool(result.stdout.strip())


def python_requirements_file() -> Path:
    """The lock wins whenever it exists — it pins the exact environment the
    release was built against. requirements.txt is only the fallback for
    installations predating the lock.

    Forward update and rollback MUST resolve this the same way. They did not
    once: rollback installed requirements.txt unconditionally, which on this
    project would have replaced the working onnxruntime-gpu with the CPU
    onnxruntime listed there — changing the recognition environment during a
    recovery, which is precisely when it must not move."""
    repo = Path(CONFIG["repo_root"])
    lock = repo / "backend" / "requirements.lock.txt"
    return lock if lock.exists() else repo / "backend" / "requirements.txt"


def python_deps_changed(previous_sha: str, target_sha: str) -> bool:
    return manifest_changed(previous_sha, target_sha, "backend/requirements.txt") or manifest_changed(
        previous_sha, target_sha, "backend/requirements.lock.txt"
    )


def do_update() -> None:
    repo = Path(CONFIG["repo_root"])
    previous_sha = CONFIG["previous_sha"]
    target_sha = CONFIG["target_sha"]
    target_tag = CONFIG["target_tag"]
    target_version = CONFIG["target_version"]

    stage("Backing up database...", "backing_up")
    backup_path = backup_database()
    write_state(backup_path=str(backup_path))

    stage(f"Downloading {target_tag}...")
    result = git("fetch", "--tags", "--prune", "origin")
    if result.returncode != 0:
        raise RuntimeError(f"git fetch failed: {redact(result.stderr)[:400]}")

    stage("Updating backend...")
    result = git("checkout", "--detach", target_sha)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout failed: {redact(result.stderr)[:400]}")

    # Integrity: prove we are on exactly the commit the release named, and
    # that its VERSION agrees with the tag. A release tagged v1.3.0 whose
    # VERSION says something else is a packaging mistake, not an update.
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != target_sha:
        raise RuntimeError(f"checked out {head[:12]}, expected {target_sha[:12]}")
    version_file = (repo / "VERSION").read_text(encoding="utf-8").strip()
    expected = target_tag.lstrip("vV")
    if version_file != expected:
        raise RuntimeError(f"VERSION file says '{version_file}' but the release tag is '{target_tag}'")
    log(f"integrity verified: HEAD={head[:12]} VERSION={version_file}")

    if python_deps_changed(previous_sha, target_sha):
        req = python_requirements_file()
        log(f"python dependencies changed; installing from {req.name}")
        result = run([CONFIG["python"], "-m", "pip", "install", "-r", str(req)], timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(f"pip install failed: {redact(result.stderr)[:400]}")
    else:
        log("python dependencies unchanged; skipping install")

    stage("Updating frontend...")
    if manifest_changed(previous_sha, target_sha, "frontend/package-lock.json") or manifest_changed(
        previous_sha, target_sha, "frontend/package.json"
    ):
        lockfile = repo / "frontend" / "package-lock.json"
        cmd = "ci" if lockfile.exists() else "install"
        log(f"node dependencies changed; running npm {cmd}")
        result = npm(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"npm {cmd} failed: {redact(result.stderr)[:400]}")
    else:
        log("node dependencies unchanged; skipping install")

    stage("Rebuilding frontend...")
    # Required, not optional: the backend serves frontend/dist, so skipping
    # this leaves the old UI running against new backend code.
    result = npm("run", "build")
    if result.returncode != 0:
        raise RuntimeError(f"frontend build failed: {redact(result.stderr)[:400]}")

    # From here on a migration can run at startup, so a rollback must restore
    # the database. Recorded durably before the restart, not after, because
    # the thing we are about to do is stop this machine's backend.
    write_state(reached_restart=True)

    stage("Restarting...", "restarting")
    stop_backend()
    start_backend()

    stage("Checking system...", "verifying")
    if not wait_for_health():
        raise RuntimeError("the updated backend did not become healthy (a migration may have failed)")
    running = reported_version()
    if running and running != target_version:
        raise RuntimeError(f"backend reports version {running}, expected {target_version}")
    log(f"health ok, version {running or '(unreported)'}")


def rollback(reason: str, backup_path: str | None) -> None:
    """Restore the previous version.

    ORDER MATTERS and is the whole point of this function:
      1. kill the failed backend and PROVE it is gone
      2. PROVE nothing still holds the database open
      3. only then restore the database
      4. then move the code back, reinstall, rebuild
      5. then start the old version again
    Restoring the file while a process still has it open either fails with a
    sharing violation (Windows) or silently corrupts the database.
    """
    log(f"ROLLBACK: {reason}")
    stage("Rolling back...", "rolling_back")
    repo = Path(CONFIG["repo_root"])
    previous_sha = CONFIG["previous_sha"]

    # 1 — the failed backend must be fully dead before anything else.
    stopped = stop_backend()
    if not stopped:
        log("WARNING: could not confirm the backend stopped; not touching the database")

    # 2 — and it must no longer hold the database.
    db_free = stopped and database_unheld()
    if not db_free:
        log("WARNING: database still held; skipping restore to avoid corrupting it")

    # 3 — restore only when both of the above are true, and only when a
    #     migration could actually have run (i.e. we got as far as restart).
    restored = False
    if backup_path and db_free and read_state().get("reached_restart"):
        restored = restore_database(Path(backup_path))
    elif backup_path and not read_state().get("reached_restart"):
        log("failure happened before restart; migrations never ran, leaving the database untouched")

    # 4 — code back to where it was.
    result = git("checkout", "--detach", previous_sha)
    if result.returncode != 0:
        log(f"WARNING: could not restore previous commit: {redact(result.stderr)[:400]}")

    try:
        if python_deps_changed(previous_sha, CONFIG["target_sha"]):
            req = python_requirements_file()
            log(f"restoring python dependencies from {req.name}")
            run([CONFIG["python"], "-m", "pip", "install", "-r", str(req)], timeout=1800)
        if manifest_changed(previous_sha, CONFIG["target_sha"], "frontend/package-lock.json"):
            npm("ci" if (repo / "frontend" / "package-lock.json").exists() else "install")
        npm("run", "build")
    except Exception as e:  # noqa: BLE001 — a rebuild problem must not stop us restarting
        log(f"rollback rebuild issue: {e}")

    # 5 — bring the previous version back up.
    start_backend()
    healthy = wait_for_health()
    write_state(
        status="rolled_back",
        stage="Update failed — previous version restored",
        error=reason,
        database_restored=restored,
        previous_version_healthy=healthy,
    )
    log(f"rollback complete (db_restored={restored}, healthy={healthy})")


def record_history(status: str, error: str = "") -> None:
    """Append the finished run to the application database for the admin UI.
    Written only at the end: an in-flight row here could be wiped by the very
    rollback it was describing."""
    try:
        conn = sqlite3.connect(CONFIG["database_path"])
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='updatejob'")
            if not cur.fetchone():
                return
            import uuid

            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            conn.execute(
                "INSERT INTO updatejob (id, from_version, to_version, target_sha, previous_sha, status, error, started_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    CONFIG["from_version"],
                    CONFIG["target_version"],
                    CONFIG["target_sha"],
                    CONFIG["previous_sha"],
                    status,
                    redact(error)[:1000] or None,
                    CONFIG.get("started_at", now),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log(f"could not record update history: {e}")


def main() -> int:
    global CONFIG, RUNTIME_DIR, STATE_FILE, LOG_FILE, LOCK_FILE

    if len(sys.argv) < 2:
        print("usage: updater.py <config.json>")
        return 2
    CONFIG = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    RUNTIME_DIR = Path(CONFIG["runtime_dir"])
    STATE_FILE = RUNTIME_DIR / "update_state.json"
    LOG_FILE = RUNTIME_DIR / "update.log"
    LOCK_FILE = RUNTIME_DIR / "update.lock"
    CONFIG["started_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")

    # Claim the lock as ours now that we have a pid to record.
    try:
        LOCK_FILE.write_text(
            json.dumps({"pid": os.getpid(), "started_at": CONFIG["started_at"]}), encoding="utf-8"
        )
    except OSError:
        pass

    write_state(
        status="preparing",
        stage="Preparing update...",
        updater_pid=os.getpid(),
        from_version=CONFIG["from_version"],
        to_version=CONFIG["target_version"],
        target_tag=CONFIG["target_tag"],
        target_sha=CONFIG["target_sha"],
        previous_sha=CONFIG["previous_sha"],
        started_at=CONFIG["started_at"],
        error="",
        reached_restart=False,
        log_path=str(LOG_FILE),
    )
    log(f"=== update {CONFIG['from_version']} -> {CONFIG['target_version']} ({CONFIG['target_tag']}) ===")

    backup_path: str | None = None
    try:
        do_update()
        write_state(status="completed", stage="Update complete", error="")
        record_history("completed")
        log("=== update complete ===")
        return 0
    except Exception as e:  # noqa: BLE001 — every failure path ends in rollback
        backup_path = read_state().get("backup_path")
        try:
            rollback(str(e), backup_path)
            record_history("rolled_back", str(e))
        except Exception as rollback_error:  # noqa: BLE001
            log(f"ROLLBACK ITSELF FAILED: {rollback_error}")
            write_state(status="failed", stage="Update failed and rollback failed", error=str(rollback_error))
            record_history("failed", f"{e} / rollback: {rollback_error}")
        return 1
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
