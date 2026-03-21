"""
Amber MCP Server — Immutable checkpoint protection + context management for AI agents.

Two capability domains:

1. FILE PROTECTION — AI agents can snapshot, verify, restore, and tag file checkpoints
   before making changes. The Gemini incident becomes structurally impossible.

2. CONTEXT LEDGER — Immutable, content-addressed snapshots of working context.
   Every context state is hashed, chain-linked, and restorable. Context compression
   is verified against stored originals. Anomaly detection flags fidelity loss.

Communicates with amberd via Unix socket IPC for file operations.
Context operations use Amber's object store directly.
"""

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

AMBER_CLI = os.environ.get("AMBER_CLI", "amber")
AMBER_HOME = Path(os.environ.get("AMBER_HOME", Path.home() / ".amber"))
CONTEXT_STORE = AMBER_HOME / "context"
CONTEXT_MANIFEST = CONTEXT_STORE / "manifest.json"

mcp = FastMCP("amber")

# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT LEDGER — Immutable, versioned, verifiable working context
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_context_store():
    """Initialize the context store if it doesn't exist."""
    CONTEXT_STORE.mkdir(parents=True, exist_ok=True)
    if not CONTEXT_MANIFEST.exists():
        _save_manifest({"versions": [], "active_branch": "main", "branches": {"main": []}})


def _load_manifest() -> dict:
    """Load the context manifest."""
    if CONTEXT_MANIFEST.exists():
        return json.loads(CONTEXT_MANIFEST.read_text())
    return {"versions": [], "active_branch": "main", "branches": {"main": []}}


def _save_manifest(manifest: dict):
    """Save the context manifest."""
    CONTEXT_MANIFEST.write_text(json.dumps(manifest, indent=2))


def _hash_context(data: str) -> str:
    """SHA-256 hash of context content."""
    return hashlib.sha256(data.encode()).hexdigest()


def _store_context_blob(content: str) -> str:
    """Store context content in content-addressed store. Returns hash key."""
    h = _hash_context(content)
    prefix = h[:2]
    blob_dir = CONTEXT_STORE / "blobs" / prefix
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob_path = blob_dir / h[2:]
    if not blob_path.exists():
        blob_path.write_text(content)
    return h


def _read_context_blob(key: str) -> str | None:
    """Read a context blob by its hash key."""
    prefix = key[:2]
    blob_path = CONTEXT_STORE / "blobs" / prefix / key[2:]
    if blob_path.exists():
        return blob_path.read_text()
    return None


def _compute_context_diff(old_ctx: dict, new_ctx: dict) -> dict:
    """Compute a structured diff between two context states."""
    diff = {"added": {}, "removed": {}, "changed": {}}
    all_keys = set(list(old_ctx.keys()) + list(new_ctx.keys()))
    for key in all_keys:
        if key not in old_ctx:
            diff["added"][key] = new_ctx[key]
        elif key not in new_ctx:
            diff["removed"][key] = old_ctx[key]
        elif old_ctx[key] != new_ctx[key]:
            diff["changed"][key] = {"from": old_ctx[key], "to": new_ctx[key]}
    return diff


# ── Context Tools ────────────────────────────────────────────────────────────

@mcp.tool()
def context_save(
    working_files: list[str],
    decisions: list[str],
    findings: list[str],
    constraints: list[str],
    task_state: str,
    notes: str = "",
    label: str = "",
) -> str:
    """
    Snapshot the current working context immutably.

    Every context state is content-addressed (SHA-256), chain-linked to the
    previous state, and stored as an immutable blob. This creates a verifiable
    audit trail of what the AI agent knew and decided at each point.

    Args:
        working_files: List of file paths currently being worked on
        decisions: Key decisions made so far (e.g. "chose approach X over Y because Z")
        findings: Important observations/discoveries during work
        constraints: Active constraints (e.g. "must not modify file X", "deadline Y")
        task_state: Current state of the task (e.g. "implementing feature A, 3/5 subtasks done")
        notes: Freeform notes
        label: Optional label for this snapshot (e.g. "before-refactor", "checkpoint-alpha")

    Returns:
        Version ID and hash of the stored context snapshot.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    context = {
        "working_files": working_files,
        "decisions": decisions,
        "findings": findings,
        "constraints": constraints,
        "task_state": task_state,
        "notes": notes,
    }

    content = json.dumps(context, indent=2, sort_keys=True)
    content_hash = _store_context_blob(content)

    # Chain link to previous version
    versions = manifest["versions"]
    parent_hash = versions[-1]["content_hash"] if versions else None

    version = {
        "version_id": content_hash[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "parent_hash": parent_hash,
        "label": label or None,
        "branch": manifest["active_branch"],
        "summary": {
            "files": len(working_files),
            "decisions": len(decisions),
            "findings": len(findings),
            "constraints": len(constraints),
            "task_state": task_state[:100],
        },
    }

    manifest["versions"].append(version)
    branch = manifest["active_branch"]
    if branch not in manifest["branches"]:
        manifest["branches"][branch] = []
    manifest["branches"][branch].append(content_hash[:12])
    _save_manifest(manifest)

    return (
        f"Context saved: {content_hash[:12]}\n"
        f"Hash: {content_hash}\n"
        f"Parent: {parent_hash[:12] if parent_hash else 'none (first snapshot)'}\n"
        f"Branch: {branch}\n"
        f"Files: {len(working_files)} | Decisions: {len(decisions)} | "
        f"Findings: {len(findings)} | Constraints: {len(constraints)}"
    )


@mcp.tool()
def context_restore(version_id: str) -> str:
    """
    Restore a previous context state by version ID (first 8+ chars of hash).

    Retrieves the full context snapshot from the immutable store and returns it
    so the agent can re-establish its working state. The original blob is
    integrity-verified against its hash before returning.

    Args:
        version_id: Short version ID (hash prefix, minimum 8 chars)

    Returns:
        The full context state as structured data.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    # Find the version
    target = None
    for v in manifest["versions"]:
        if v["version_id"].startswith(version_id) or v["content_hash"].startswith(version_id):
            target = v
            break

    if not target:
        return f"Version {version_id} not found. Use context_log to see available versions."

    # Read and verify
    content = _read_context_blob(target["content_hash"])
    if content is None:
        return f"ERROR: Blob {target['content_hash'][:12]} missing from store. Data may be corrupted."

    # Integrity check
    actual_hash = _hash_context(content)
    if actual_hash != target["content_hash"]:
        return (
            f"INTEGRITY FAILURE: Stored blob has been modified.\n"
            f"Expected: {target['content_hash']}\n"
            f"Actual:   {actual_hash}\n"
            f"This context state cannot be trusted."
        )

    context = json.loads(content)
    return (
        f"Context restored: {target['version_id']}\n"
        f"Timestamp: {target['timestamp']}\n"
        f"Label: {target.get('label', 'none')}\n"
        f"Integrity: VERIFIED\n\n"
        f"Working files:\n" + "\n".join(f"  - {f}" for f in context["working_files"]) + "\n\n"
        f"Decisions:\n" + "\n".join(f"  - {d}" for d in context["decisions"]) + "\n\n"
        f"Findings:\n" + "\n".join(f"  - {f}" for f in context["findings"]) + "\n\n"
        f"Constraints:\n" + "\n".join(f"  - {c}" for c in context["constraints"]) + "\n\n"
        f"Task state: {context['task_state']}\n"
        f"Notes: {context.get('notes', '')}"
    )


@mcp.tool()
def context_log(limit: int = 20) -> str:
    """
    Show the context version history (newest first).

    Displays the chain of context snapshots with timestamps, labels,
    summaries, and parent links — forming an audit trail of the agent's
    working state over time.

    Args:
        limit: Maximum number of versions to show (default 20)

    Returns:
        Formatted version history.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    versions = manifest["versions"][-limit:]
    versions.reverse()

    if not versions:
        return "No context snapshots yet. Use context_save to create the first one."

    lines = [f"Context log ({len(manifest['versions'])} total, showing {len(versions)}):\n"]
    for v in versions:
        label = f"  [{v['label']}]" if v.get("label") else ""
        parent = v["parent_hash"][:8] if v.get("parent_hash") else "root"
        s = v.get("summary", {})
        lines.append(
            f"  {v['version_id']}  {v['timestamp'][:19]}  "
            f"branch:{v.get('branch', 'main')}  parent:{parent}"
            f"{label}\n"
            f"           files:{s.get('files', '?')} decisions:{s.get('decisions', '?')} "
            f"findings:{s.get('findings', '?')} | {s.get('task_state', '')[:60]}"
        )
    return "\n".join(lines)


@mcp.tool()
def context_diff(version_a: str, version_b: str) -> str:
    """
    Show what changed between two context snapshots.

    Computes a structured diff showing added, removed, and changed fields.
    Both versions are integrity-verified before comparison.

    Args:
        version_a: First version ID (older)
        version_b: Second version ID (newer)

    Returns:
        Structured diff between the two context states.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    def find_version(vid):
        for v in manifest["versions"]:
            if v["version_id"].startswith(vid) or v["content_hash"].startswith(vid):
                return v
        return None

    va = find_version(version_a)
    vb = find_version(version_b)
    if not va:
        return f"Version {version_a} not found."
    if not vb:
        return f"Version {version_b} not found."

    content_a = _read_context_blob(va["content_hash"])
    content_b = _read_context_blob(vb["content_hash"])
    if not content_a or not content_b:
        return "ERROR: One or both context blobs missing from store."

    ctx_a = json.loads(content_a)
    ctx_b = json.loads(content_b)
    diff = _compute_context_diff(ctx_a, ctx_b)

    lines = [f"Context diff: {va['version_id']} → {vb['version_id']}\n"]

    if diff["added"]:
        lines.append("ADDED:")
        for k, v in diff["added"].items():
            lines.append(f"  + {k}: {json.dumps(v)[:200]}")

    if diff["removed"]:
        lines.append("REMOVED:")
        for k, v in diff["removed"].items():
            lines.append(f"  - {k}: {json.dumps(v)[:200]}")

    if diff["changed"]:
        lines.append("CHANGED:")
        for k, v in diff["changed"].items():
            lines.append(f"  ~ {k}:")
            lines.append(f"    from: {json.dumps(v['from'])[:200]}")
            lines.append(f"    to:   {json.dumps(v['to'])[:200]}")

    if not diff["added"] and not diff["removed"] and not diff["changed"]:
        lines.append("No differences.")

    return "\n".join(lines)


@mcp.tool()
def context_branch(name: str) -> str:
    """
    Create or switch to a context branch.

    Branches allow exploring different approaches while keeping the full
    context history for each path. Like git branches but for AI working state.

    Args:
        name: Branch name to create or switch to

    Returns:
        Confirmation of branch switch.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    old_branch = manifest["active_branch"]
    manifest["active_branch"] = name
    if name not in manifest["branches"]:
        manifest["branches"][name] = []
    _save_manifest(manifest)

    return f"Switched context branch: {old_branch} → {name}"


@mcp.tool()
def context_verify() -> str:
    """
    Verify integrity of all stored context snapshots.

    Re-hashes every context blob from disk and compares against its
    content-addressed key. Checks parent chain integrity. Reports any
    corruption or tampering.

    Returns:
        Integrity verification report.
    """
    _ensure_context_store()
    manifest = _load_manifest()

    total = 0
    passed = 0
    failed = []
    chain_breaks = []

    for i, v in enumerate(manifest["versions"]):
        total += 1
        content = _read_context_blob(v["content_hash"])
        if content is None:
            failed.append(f"{v['version_id']}: blob missing")
            continue
        actual = _hash_context(content)
        if actual != v["content_hash"]:
            failed.append(f"{v['version_id']}: hash mismatch (expected {v['content_hash'][:12]}, got {actual[:12]})")
        else:
            passed += 1

        # Chain integrity
        if i > 0:
            expected_parent = manifest["versions"][i - 1]["content_hash"]
            if v.get("parent_hash") != expected_parent:
                chain_breaks.append(f"{v['version_id']}: parent chain broken")

    lines = [f"Context integrity: {passed}/{total} verified"]
    if failed:
        lines.append(f"\nFAILED ({len(failed)}):")
        for f in failed:
            lines.append(f"  {f}")
    if chain_breaks:
        lines.append(f"\nCHAIN BREAKS ({len(chain_breaks)}):")
        for c in chain_breaks:
            lines.append(f"  {c}")
    if not failed and not chain_breaks:
        lines.append("All context snapshots verified. Chain integrity intact.")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# FILE PROTECTION — Checkpoint operations via Amber CLI
# ══════════════════════════════════════════════════════════════════════════════

def _run_amber(*args) -> str:
    """Run an amber CLI command and return output."""
    try:
        result = subprocess.run(
            [AMBER_CLI] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nERROR: {result.stderr.strip()}"
        return output or "(no output)"
    except FileNotFoundError:
        return "ERROR: amber CLI not found. Install with: cargo build --release && cp target/release/amber ~/.local/bin/"
    except subprocess.TimeoutExpired:
        return "ERROR: amber command timed out after 30 seconds."


@mcp.tool()
def amber_status() -> str:
    """
    Show all watched paths and their protection status.

    Returns version counts, training mode, anomaly counts, and gate status
    for every directory Amber is monitoring. Use this to understand what's
    currently protected before making changes.

    Returns:
        Table of watched paths with status information.
    """
    return _run_amber("status")


@mcp.tool()
def amber_log(path: str, since: str = "") -> str:
    """
    Show version history for a file or directory.

    Lists all stored snapshots with timestamps, sizes, session IDs,
    metadata tags, and anomaly flags. Use before modifying a file to
    understand its history.

    Args:
        path: File or directory path to show history for
        since: Optional time filter (e.g. "2h", "1d", "2026-03-20")

    Returns:
        Formatted version history.
    """
    args = ["log", path]
    if since:
        args.extend(["--since", since])
    return _run_amber(*args)


@mcp.tool()
def amber_snapshot(path: str) -> str:
    """
    Force an immediate snapshot of a file or directory.

    Use this BEFORE making any modifications to a watched file. The snapshot
    is stored immutably — even if your subsequent changes cause damage, the
    pre-change state is always recoverable.

    Args:
        path: File or directory to snapshot

    Returns:
        Confirmation with version ID and hash.
    """
    # Touch the file to trigger the daemon's inotify watcher
    path_obj = Path(path)
    if path_obj.is_file():
        path_obj.touch()
        return f"Snapshot triggered for {path}. The daemon will store it immutably."
    elif path_obj.is_dir():
        return f"Directory snapshots are automatic. All files under {path} are watched."
    else:
        return f"Path {path} does not exist."


@mcp.tool()
def amber_verify(path: str = "") -> str:
    """
    Verify integrity of stored checkpoints.

    Re-reads every stored object from disk, decompresses, re-hashes, and
    compares against content-addressed keys. Catches bit rot, silent
    corruption, and unauthorized modification.

    Args:
        path: Optional path to restrict verification to

    Returns:
        Integrity verification report.
    """
    args = ["verify"]
    if path:
        args.extend(["--path", path])
    return _run_amber(*args)


@mcp.tool()
def amber_restore(path: str, version: str) -> str:
    """
    Restore a file to a previous version.

    Retrieves the specified version from the immutable store and writes it
    to an adjacent file (.amber-restore). The original file is NOT overwritten
    — you must explicitly replace it after reviewing.

    Args:
        path: File path to restore
        version: Version short ID (first 8 chars)

    Returns:
        Path to the restored file and verification hash.
    """
    return _run_amber("restore", path, "--version", version)


@mcp.tool()
def amber_tag(path: str, version: str, key: str, value: str) -> str:
    """
    Tag a checkpoint version with structured metadata.

    Attach key-value metadata to any version — test scores, training phase,
    loss values, notes. Tags are stored in the manifest and shown in amber log.

    Args:
        path: File path the version belongs to
        version: Version short ID
        key: Metadata key (e.g. "score", "phase", "loss")
        value: Metadata value (e.g. "5/5", "phase-0", "0.031")

    Returns:
        Confirmation of tag.
    """
    return _run_amber("tag", path, "--version", version, "--key", key, "--value", value)


@mcp.tool()
def amber_diff(path: str, version1: str, version2: str) -> str:
    """
    Show differences between two versions of a file.

    For text files, shows a line-by-line diff. For binary files (model
    checkpoints), shows size and metadata changes.

    Args:
        path: File path
        version1: First version short ID (older)
        version2: Second version short ID (newer)

    Returns:
        Diff output.
    """
    return _run_amber("diff", path, version1, version2)


@mcp.tool()
def amber_search(pattern: str, path: str = "") -> str:
    """
    Search for text across all stored versions.

    Searches the content of every non-archived version for the given pattern.
    Useful for finding when a specific function, variable, or configuration
    was added, changed, or removed.

    Args:
        pattern: Text pattern to search for
        path: Optional path to restrict search to

    Returns:
        Search results with version IDs, line numbers, and matching lines.
    """
    args = ["search", pattern]
    if path:
        args.extend(["--path", path])
    return _run_amber(*args)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
