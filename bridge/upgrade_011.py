"""Back up and prepare the narrow 0.1.0 -> 0.1.1 WebDev library update."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import hmac
import io
import json
import os
import secrets
import select
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

LIBRARY = "ignition/script-python/mcp_bridge_lib/code.py"
WEBDEV = "com.inductiveautomation.webdev/resources/api"
CANDIDATE = Path(__file__).with_name("baselines") / "mcp_bridge_lib_0_1_2.py"
PLACEHOLDER = "__BRIDGE_SECRET__"
HEADER = b"IGNITION-MCP-BRIDGE-BACKUP-v1\x00"
SCOPED = ("tags/scoped-state", "tags/scoped-configure", "tags/scoped-restore")
IMPORTS = ("math", "re", "threading")
CONSTANTS = (
    "TAG_CONFIG_ROOTS", "TAG_CONFIG_LOCK", "TAG_HISTORY_PROPERTIES",
    "TAG_HISTORY_CHOICES", "TAG_READ_PROPERTIES",
)
FUNCTIONS = (
    "_scoped_tag_path", "_scoped_find_child", "_scoped_tag_snapshot",
    "handle_tags_scoped_state", "_scoped_result", "_valid_history_patch",
    "handle_tags_scoped_configure", "handle_tags_scoped_restore",
)
PERSPECTIVE = ("files/list", "files/read", "files/write", "files/delete")
MAX_ZIP_BYTES = 32 * 1024 * 1024


class UpgradeError(Exception):
    """Safe operator error that does not include project contents."""


def _check(condition, message):
    if not condition:
        raise UpgradeError(message)


def _project(data):
    _check(len(data) <= MAX_ZIP_BYTES, "Export exceeds the supported size")
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        paths = [entry.filename.rstrip("/") for entry in entries]
        _check(len(paths) == len(set(paths)), "Export contains repeated entries")
        _check(sum(entry.file_size for entry in entries) <= MAX_ZIP_BYTES, "Export is too large")
        for entry, path in zip(entries, paths):
            parsed = PurePosixPath(path)
            _check(
                path and not parsed.is_absolute() and ".." not in parsed.parts
                and "\\" not in path and not stat.S_ISLNK(entry.external_attr >> 16),
                "Export contains an unsafe path",
            )
        _check(archive.testzip() is None, "Export ZIP failed CRC verification")
        project = {entry.filename: archive.read(entry) for entry in entries if not entry.is_dir()}
    required = {LIBRARY, "project.json", f"{WEBDEV}/config.json",
                f"{WEBDEV}/resource.json", f"{WEBDEV}/doPost",
                str(PurePosixPath(LIBRARY).with_name("resource.json"))}
    _check(required <= project.keys(), "Missing bridge library, project manifest or WebDev api")
    _check(isinstance(json.loads(project["project.json"]), dict), "Invalid project metadata")
    for path, content in project.items():
        if path.endswith("/resource.json"):
            names = json.loads(content).get("files")
            parent = str(PurePosixPath(path).parent)
            _check(isinstance(names, list) and all(f"{parent}/{name}" in project for name in names),
                   "A resource manifest refers to an absent file")
    return project


def _assignments(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                result.setdefault(target.id, []).append(node)
    return result


def _one(mapping, key):
    items = mapping.get(key, [])
    _check(len(items) == 1, f"Expected one declaration of {key}")
    return items[0]


def _constant(mapping, name):
    node = _one(mapping, name)
    _check(node.lineno == node.end_lineno, f"{name} must occupy one line")
    value = ast.literal_eval(node.value)
    _check(isinstance(value, str), f"{name} must be a string constant")
    return value


def _library(data):
    code = data.decode("utf-8")
    tree = ast.parse(code)
    assignments = _assignments(tree)
    return code, tree, assignments, _constant(assignments, "BRIDGE_SECRET")


def _handlers(assignments):
    node = _one(assignments, "HANDLERS").value
    _check(isinstance(node, ast.Dict), "Unsupported HANDLERS declaration")
    routes = dict(zip((ast.literal_eval(key) for key in node.keys), node.values))
    _check(len(routes) == len(node.keys), "Duplicate bridge routes")
    return routes, node


def _functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _minimal_update(raw, candidate):
    code, tree, current, secret = _library(raw)
    new_code, new_tree, new, _ = _library(candidate)
    _check(_constant(current, "BRIDGE_VERSION") == "0.1.0", "Gateway library is not 0.1.0")
    _check(_constant(new, "BRIDGE_VERSION") == "0.1.1", "Candidate library is not 0.1.1")
    current_routes, _ = _handlers(current)
    new_routes, _ = _handlers(new)
    _check(set(PERSPECTIVE) <= current_routes.keys(), "A Perspective file capability is missing")
    _check(not set(SCOPED) & current_routes.keys(), "Scoped routes already exist")
    _check(not set(CONSTANTS) & current.keys(), "Scoped constant names already exist")
    old_fns, new_fns = _functions(tree), _functions(new_tree)
    _check(not set(FUNCTIONS) & old_fns.keys(), "Scoped function names already exist")
    lines = code.splitlines(keepends=True)
    candidate_lines = new_code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"

    def excerpt(node):
        return "".join(candidate_lines[node.lineno - 1:node.end_lineno]).rstrip("\r\n")

    imported = {alias.name for node in tree.body if isinstance(node, ast.Import)
                for alias in node.names if alias.asname is None}
    version = _one(current, "BRIDGE_VERSION").lineno - 1
    additions = (
        (version, version + 1, "".join(f"import {name}{eol}" for name in IMPORTS
                                      if name not in imported) + f"BRIDGE_VERSION = '0.1.1'{eol}"),
        (_one(current, "MAX_SKEW_SECONDS").end_lineno,
         _one(current, "MAX_SKEW_SECONDS").end_lineno,
         eol.join(excerpt(_one(new, name)) for name in CONSTANTS) + eol),
        (old_fns["handle_tags_write"].lineno - 1, old_fns["handle_tags_write"].lineno - 1,
         (eol * 3).join(excerpt(new_fns[name]) for name in FUNCTIONS) + eol * 3),
        (current_routes["tags/configure"].end_lineno, current_routes["tags/configure"].end_lineno,
         "".join(f"    {route!r}: {new_routes[route].id},{eol}" for route in SCOPED)),
    )
    for start, end, change in sorted(additions, reverse=True):
        lines[start:end] = [change.replace("\r\n", "\n").replace("\n", eol)]
    result = "".join(lines).encode()
    _, updated_tree, updated, updated_secret = _library(result)
    updated_routes, _ = _handlers(updated)
    updated_fns = _functions(updated_tree)
    _check(secret == updated_secret and set(updated_routes) == set(current_routes) | set(SCOPED),
           "Update did not preserve the secret or the original routes")
    for name in current_routes:
        _check(ast.dump(current_routes[name]) == ast.dump(updated_routes[name]),
               "An existing route was redirected")
    for name in old_fns:
        _check(ast.dump(old_fns[name]) == ast.dump(updated_fns[name]),
               "An existing function was changed")
    _check(_constant(updated, "BRIDGE_VERSION") == "0.1.1", "Update version mismatch")
    return result


def _archive_key(secret, salt):
    _check(secret != PLACEHOLDER and len(secret.encode()) >= 32,
           "Recovery requires the configured bridge secret of at least 32 bytes")
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), HEADER + salt, 300_000, dklen=64)


def _openssl(data, key, decrypt=False):
    env = dict(os.environ)
    env.pop("IGNITION_BRIDGE_SECRET", None)
    env.pop("IGNITION_API_TOKEN", None)
    env["MCP_BRIDGE_BACKUP_PASS"] = key.hex()
    result = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
         "-md", "sha256", "-pass", "env:MCP_BRIDGE_BACKUP_PASS"] + (["-d"] if decrypt else []),
        input=data, capture_output=True, env=env, timeout=30, check=False,
    )
    _check(result.returncode == 0, "OpenSSL backup encryption or decryption failed")
    return result.stdout


def _seal(raw, secret):
    salt = secrets.token_bytes(16)
    keys = _archive_key(secret, salt)
    body = HEADER + salt + _openssl(raw, keys[:32])
    return body + hmac.digest(keys[32:], body, "sha256")


def _unseal(data, secret):
    _check(data.startswith(HEADER) and len(data) > len(HEADER) + 48, "Invalid backup format")
    salt = data[len(HEADER):len(HEADER) + 16]
    body, signature = data[:-32], data[-32:]
    keys = _archive_key(secret, salt)
    _check(hmac.compare_digest(hmac.digest(keys[32:], body, "sha256"), signature),
           "Backup authentication failed")
    return _openssl(body[len(HEADER) + 16:], keys[:32], decrypt=True)


def _save_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _read_fifo(path, timeout=45):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        _check(stat.S_ISFIFO(os.fstat(fd).st_mode), "Export destination must be a FIFO")
        deadline = time.monotonic() + timeout
        data = bytearray()
        print("FIFO_READY", flush=True)
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], min(0.25, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if chunk:
                data.extend(chunk)
                _check(len(data) <= MAX_ZIP_BYTES, "Export exceeded the supported size")
            elif data:
                return bytes(data)
            else:
                time.sleep(0.05)
        raise UpgradeError("No complete export arrived on the FIFO")
    finally:
        os.close(fd)


def _redacted(data):
    code, _, assignments, _ = _library(data)
    lines = code.splitlines(keepends=True)
    i = _one(assignments, "BRIDGE_SECRET").lineno - 1
    lines[i] = "BRIDGE_SECRET = '<redacted>'\n"
    return "".join(lines)


def _delta(library, updated):
    return "".join(difflib.unified_diff(
        _redacted(library).splitlines(keepends=True),
        _redacted(updated).splitlines(keepends=True),
        fromfile=f"a/{LIBRARY}", tofile=f"b/{LIBRARY}", n=0,
    )).encode()


def capture(args):
    _check(args.backup_only or args.patch is not None, "Supply --patch unless using --backup-only")
    raw = _read_fifo(args.fifo)
    project = _project(raw)
    library = project[LIBRARY]
    _, _, assignments, secret = _library(library)
    _save_private(args.backup, _seal(raw, secret))
    restored = _unseal(args.backup.read_bytes(), secret)
    _check(restored == raw, "Backup verification failed")
    _project(restored)
    print(json.dumps({"backup": str(args.backup), "zip_integrity": True, "members": len(project),
                      "encrypted_sha256": hashlib.sha256(args.backup.read_bytes()).hexdigest(),
                      "bridge_version": _constant(assignments, "BRIDGE_VERSION")}), flush=True)
    if args.backup_only:
        return
    updated = _minimal_update(library, args.candidate.read_bytes())
    _save_private(args.patch, _delta(library, updated))
    print(json.dumps({"changed_resource": LIBRARY, "new_routes": SCOPED,
                      "retained_routes": len(_handlers(assignments)[0]),
                      "webdev_api_untouched": True, "review_patch": str(args.patch)}))


def audit(args):
    project = _project(_read_fifo(args.fifo))
    library = project[LIBRARY]
    _, live_tree, assignments, secret = _library(library)
    backup = _project(_unseal(args.backup.read_bytes(), secret))
    _check(backup == project, "Current Gateway resource contents no longer match the backup")
    candidate = args.candidate.read_bytes()
    _, local_tree, local_assignments, _ = _library(candidate)
    updated = _minimal_update(library, candidate)
    _check(args.patch.read_bytes() == _delta(library, updated), "Prepared patch does not match")
    original, local = _functions(live_tree), _functions(local_tree)
    changes_from_live = sorted(name for name in original if name not in local
                               or ast.dump(original[name]) != ast.dump(local[name]))
    routes = _handlers(assignments)[0]
    print(json.dumps({
        "backup_matches_current_gateway": True,
        "live": _constant(assignments, "BRIDGE_VERSION"),
        "candidate": _constant(local_assignments, "BRIDGE_VERSION"),
        "existing_routes_retained": len(routes),
        "existing_functions_retained": len(original),
        "functions_different_in_full_local_source": changes_from_live,
        "perspective_routes_retained": PERSPECTIVE,
        "hmac_verification_retained": "_verify" in original,
        "webdev_doPost_matches_local": (
            project[f"{WEBDEV}/doPost"] == args.candidate.with_name("doPost.py").read_bytes()
        ),
    }))


def _replace_library(path, before, after):
    fd, name = tempfile.mkstemp(prefix=".bridge-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(after)
            file.flush()
            os.fsync(file.fileno())
        _check(path.read_bytes() == before, "Library changed during preparation")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
            replace.restype = wintypes.BOOL
            if not replace(str(path.resolve()), str(Path(name).resolve()), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            original = path.stat()
            _check(original.st_uid == os.geteuid(), "Run as the Gateway file owner")
            if os.stat(name).st_gid != original.st_gid:
                os.chown(name, original.st_uid, original.st_gid)
            os.chmod(name, stat.S_IMODE(original.st_mode))
            os.replace(name, path)
        _check(path.read_bytes() == after, "Library readback differs")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def activate(args):
    path = args.projects_dir / "mcp-bridge" / LIBRARY
    _check(args.projects_dir.name == "projects" and path.is_file()
           and path.resolve() == args.projects_dir.resolve() / "mcp-bridge" / LIBRARY,
           "Select the Gateway data/projects directory with an ordinary mcp-bridge library")
    current = path.read_bytes()
    _, _, _, secret = _library(current)
    backup = _project(_unseal(args.backup.read_bytes(), secret))
    saved = backup[LIBRARY]
    updated = _minimal_update(saved, args.candidate.read_bytes())
    _check(args.patch.read_bytes() == _delta(saved, updated), "Prepared patch does not match")
    parent = args.projects_dir / "mcp-bridge"
    for name, content in backup.items():
        if name.startswith(WEBDEV + "/"):
            _check((parent / name).read_bytes() == content, "WebDev api changed since backup")
    expected, desired = (updated, saved) if args.rollback else (saved, updated)
    _check(current == expected, "On-disk library differs from the reviewed state")
    if args.rollback and args.write:
        _check(args.current_backup is not None, "Rollback requires a backup of the current bridge")
        current_backup = _project(_unseal(args.current_backup.read_bytes(), secret))
        _check(current_backup[LIBRARY] == current, "Rollback backup does not match the current library")
    if args.write:
        _replace_library(path, current, desired)
    print(json.dumps({"write_applied": args.write, "resource": LIBRARY,
                      "target_version": "0.1.0" if args.rollback else "0.1.1"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("capture", help="validate and encrypt a project export via a FIFO")
    take.add_argument("--fifo", type=Path, required=True)
    take.add_argument("--patch", type=Path)
    take.add_argument("--backup-only", action="store_true")
    compare = commands.add_parser("audit", help="compare a fresh export with the encrypted backup")
    compare.add_argument("--fifo", type=Path, required=True)
    compare.add_argument("--patch", type=Path, required=True)
    operator = commands.add_parser("activate", help="check the single resource on the Gateway host")
    operator.add_argument("--projects-dir", type=Path, required=True)
    operator.add_argument("--patch", type=Path, required=True)
    operator.add_argument("--current-backup", type=Path)
    operator.add_argument("--write", action="store_true")
    operator.add_argument("--rollback", action="store_true")
    for subparser in (take, compare, operator):
        subparser.add_argument("--backup", type=Path, required=True)
        subparser.add_argument("--candidate", type=Path, default=CANDIDATE)
    args = parser.parse_args()
    try:
        {"capture": capture, "audit": audit, "activate": activate}[args.command](args)
    except UpgradeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, AttributeError, SyntaxError, BadZipFile,
            subprocess.SubprocessError) as exc:
        print(f"Bridge preparation stopped ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
