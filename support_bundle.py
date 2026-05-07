"""
Support bundle generator.

When a Cardomancer instance hits a problem the developer can't reach,
the user clicks "Download support bundle" in Settings and gets a zip
they can email/share. The bundle is the first thing we ask for in any
support thread, so it has to:

- Include the most recent app log (logs/card_sorter.log + rotated copies)
- Include enough recent scan_logs/ entries to debug a CV / sort issue
  without including gigabytes of historical scans
- Capture version info (config.APP_NAME, git HEAD if available)
- Capture machine config (bin positions, camera offset, drop heights)
- Snapshot of the runtime state (worker.state, hardware connection)
- System info (OS, Python version)
- A redacted copy of any persisted JSON state (no secrets in this app
  today, but the redactor is in place for when we add API keys)

Critical: NEVER include collection.db (PII / value data),
default-cards-*.json (large + obtainable from Scryfall), or any
hash_db / embedding_db files (large + obtainable from rebuilding).
"""

import io
import json
import os
import platform
import sys
import time
import zipfile
from datetime import datetime
from typing import Optional


# Files we'll include from the project root if they exist. None of these
# contain secrets today, but each has been considered for inclusion
# explicitly rather than including everything by default.
_INCLUDE_PROJECT_FILES = [
    "_last_setup.json",
    "drop_height.json",
    "empty_source_z.json",
    "staging_roi.json",
    "bounding_box.json",
    "printings_map.json",  # small index, useful for debugging routing
]


def _read_file_safely(path: str, max_bytes: int = 5 * 1024 * 1024) -> Optional[bytes]:
    """Read a file, capping at max_bytes (default 5MB). Returns None
    if the file can't be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            if st.st_size > max_bytes:
                # Read the LAST max_bytes — for log files the recent stuff
                # is what matters.
                f.seek(-max_bytes, os.SEEK_END)
            return f.read()
    except OSError:
        return None


def _redact_for_bundle(text: str) -> str:
    """Hook for future redaction. Today nothing in this app stores
    secrets, but when we add Moxfield tokens / S3 keys / etc. they
    pass through here on their way into the bundle."""
    # No secrets today. This function is the seam where:
    #   - re.sub for tokens ("token=..." → "token=<REDACTED>")
    #   - removal of API key headers
    # will land. Keeping it as a no-op so wiring is in place.
    return text


def _git_head_sha(repo_root: str) -> Optional[str]:
    """Return the short SHA of HEAD, if this is a git checkout. Handles
    both regular checkouts (.git is a directory) and git worktrees
    (.git is a file with `gitdir: <path>` pointing into the parent
    repo's .git/worktrees/<name>/)."""
    git_marker = os.path.join(repo_root, ".git")
    git_dir: Optional[str] = None
    try:
        if os.path.isdir(git_marker):
            git_dir = git_marker
        elif os.path.isfile(git_marker):
            with open(git_marker, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                pointed = content.split("gitdir:", 1)[1].strip()
                if not os.path.isabs(pointed):
                    pointed = os.path.normpath(os.path.join(repo_root, pointed))
                git_dir = pointed
    except OSError:
        return None
    if not git_dir:
        return None

    head_path = os.path.join(git_dir, "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None

    if head.startswith("ref:"):
        ref = head.split("ref:", 1)[1].strip()
        # Follow into git_dir first, then fall back to the parent
        # `commondir` (worktrees keep refs in the parent repo).
        candidates = [os.path.join(git_dir, ref)]
        commondir = os.path.join(git_dir, "commondir")
        try:
            with open(commondir, "r", encoding="utf-8") as f:
                cd = f.read().strip()
            if not os.path.isabs(cd):
                cd = os.path.normpath(os.path.join(git_dir, cd))
            candidates.append(os.path.join(cd, ref))
        except OSError:
            pass
        for ref_path in candidates:
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    sha = f.read().strip()
                if sha and len(sha) >= 7:
                    return sha[:12]
            except OSError:
                continue
        # Detached HEAD or packed ref — fall through
        return None
    if len(head) >= 7:
        return head[:12]
    return None


def _machine_state_snapshot(worker) -> dict:
    """Best-effort runtime state. Defensive against the worker not
    having every attribute (e.g. if the bundle is requested before
    a session has ever started)."""
    snap = {
        "state": getattr(worker, "_state", None) or getattr(worker, "state", None),
        "tracker_active": getattr(worker, "tracker", None) is not None,
        "continuous_sorting": getattr(worker, "continuous_sorting", None),
        "scan_count": getattr(worker, "scan_count", None),
        "rehome_interval": getattr(worker, "rehome_interval", None),
        "wishlist_bin": getattr(worker, "wishlist_bin", None),
        "priority_bin": getattr(worker, "priority_bin", None),
        "bin_card_limit": getattr(worker, "bin_card_limit", None),
        "bins_full": sorted(getattr(worker, "bins_full", set()) or set()),
        "bin_card_counts": getattr(worker, "bin_card_counts", None),
        "overflow_map": getattr(worker, "overflow_map", None),
        "last_card_bin": getattr(worker, "last_card_bin", None),
        "last_card_method": getattr(worker, "last_card_method", None),
    }
    return snap


def _hardware_state_snapshot() -> dict:
    """Capture what gcode_control knows about the hardware. Lazy-imported
    so that an ImportError in the gcode stack (e.g. pyserial missing)
    doesn't break bundle generation."""
    try:
        import gcode_control
    except Exception as e:
        return {"import_error": str(e)}
    out: dict = {}
    try:
        out["serial_port"] = getattr(gcode_control, "SERIAL_PORT", None)
        out["is_connected"] = bool(gcode_control.is_connected())
        out["bin_locations"] = gcode_control.get_bin_locations() or {}
        out["x_source_bin"] = getattr(gcode_control, "X_SOURCE_BIN", None)
        out["x_staging_position"] = getattr(gcode_control, "X_STAGING_POSITION", None)
        out["camera_x_offset"] = getattr(gcode_control, "CAMERA_X_OFFSET", None)
        out["last_serial_error"] = getattr(gcode_control, "_last_serial_error", None)
    except Exception as e:
        out["snapshot_error"] = str(e)
    return out


def _system_info() -> dict:
    """OS / Python / process info that's commonly relevant in a
    support thread. None of this is sensitive."""
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cwd": os.getcwd(),
        "argv": sys.argv,
        "pid": os.getpid(),
    }


def _list_recent_scan_session_dirs(scan_logs_dir: str, limit: int = 3) -> list:
    """Return the N most recent session_* directories under scan_logs/.
    Sessions are timestamped so directory name sort = chronological."""
    try:
        entries = [
            e for e in os.listdir(scan_logs_dir)
            if e.startswith("session_") and os.path.isdir(
                os.path.join(scan_logs_dir, e)
            )
        ]
    except OSError:
        return []
    entries.sort(reverse=True)
    return entries[:limit]


def build_support_bundle(
    repo_root: str,
    worker,
    *,
    app_name: str = "Cardomancer",
    log_dir_name: str = "logs",
    scan_logs_dir_name: str = "scan_logs",
    recent_sessions: int = 3,
) -> bytes:
    """
    Produce a zip (as bytes) ready to download.

    Layout inside the zip:
        <app_name>-support-<timestamp>/
            manifest.json           — bundle version, generation timestamp
            app_state.json          — worker + hardware + system snapshot
            logs/                   — rotating app logs
                card_sorter.log
                card_sorter.log.1
                ...
            scan_logs/              — last N session directories
                session_YYYYMMDD_HHMMSS/...
            project_files/          — selected JSONs from project root
                _last_setup.json
                drop_height.json
                ...
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_root = f"{app_name.lower()}-support-{timestamp}"
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # --- manifest.json ---
        manifest = {
            "bundle_version": 1,
            "app_name": app_name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "git_sha": _git_head_sha(repo_root),
        }
        zf.writestr(
            f"{bundle_root}/manifest.json",
            json.dumps(manifest, indent=2),
        )

        # --- app_state.json ---
        state = {
            "system": _system_info(),
            "worker": _machine_state_snapshot(worker),
            "hardware": _hardware_state_snapshot(),
        }
        try:
            zf.writestr(
                f"{bundle_root}/app_state.json",
                _redact_for_bundle(json.dumps(state, indent=2, default=str)),
            )
        except Exception as e:
            zf.writestr(
                f"{bundle_root}/app_state_error.txt",
                f"Could not serialize app state: {e}",
            )

        # --- logs/ ---
        log_dir = os.path.join(repo_root, log_dir_name)
        if os.path.isdir(log_dir):
            for entry in os.listdir(log_dir):
                full = os.path.join(log_dir, entry)
                if not os.path.isfile(full):
                    continue
                data = _read_file_safely(full, max_bytes=10 * 1024 * 1024)
                if data is None:
                    continue
                try:
                    decoded = data.decode("utf-8", errors="replace")
                    decoded = _redact_for_bundle(decoded)
                    zf.writestr(f"{bundle_root}/logs/{entry}",
                                decoded.encode("utf-8"))
                except Exception:
                    zf.writestr(f"{bundle_root}/logs/{entry}", data)

        # --- scan_logs/ (last N sessions only) ---
        scan_logs_dir = os.path.join(repo_root, scan_logs_dir_name)
        if os.path.isdir(scan_logs_dir):
            for session_name in _list_recent_scan_session_dirs(
                    scan_logs_dir, limit=recent_sessions):
                session_path = os.path.join(scan_logs_dir, session_name)
                for root, _dirs, files in os.walk(session_path):
                    for fname in files:
                        full = os.path.join(root, fname)
                        rel = os.path.relpath(full, scan_logs_dir)
                        # Skip large frame directories (frames/ holds
                        # JPGs which can be hundreds of MB per session)
                        # to keep the bundle email-able. The CSV +
                        # JSON metadata is what's most useful for
                        # debugging anyway.
                        if os.sep + "frames" + os.sep in full:
                            continue
                        data = _read_file_safely(full, max_bytes=2 * 1024 * 1024)
                        if data is None:
                            continue
                        zf.writestr(f"{bundle_root}/scan_logs/{rel}", data)

        # --- project_files/ ---
        for fname in _INCLUDE_PROJECT_FILES:
            full = os.path.join(repo_root, fname)
            if not os.path.isfile(full):
                continue
            data = _read_file_safely(full, max_bytes=5 * 1024 * 1024)
            if data is None:
                continue
            zf.writestr(f"{bundle_root}/project_files/{fname}", data)

        # --- README.txt for whoever opens this ---
        readme = (
            f"{app_name} support bundle generated "
            f"{manifest['generated_at']}.\n\n"
            f"Contents:\n"
            f"  manifest.json     bundle version + git SHA\n"
            f"  app_state.json    runtime + hardware + system snapshot\n"
            f"  logs/             rotating app logs\n"
            f"  scan_logs/        last {recent_sessions} session(s), "
            f"frames/ excluded\n"
            f"  project_files/    selected JSONs from the project root\n"
            f"\n"
            f"This bundle does NOT include the SQLite collection database, "
            f"the Scryfall card metadata download, or any hash/embedding "
            f"databases. Those are large and reproducible — they don't "
            f"belong in a support email.\n"
        )
        zf.writestr(f"{bundle_root}/README.txt", readme)

    return buf.getvalue()
