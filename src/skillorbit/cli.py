"""Local-only skill inventory, Markdown reconciliation, and process lifecycle."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import html
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import uuid

import yaml

from . import __version__

BEGIN = "<!-- skillorbit:begin -->"
END = "<!-- skillorbit:end -->"
CONFIG = ".skillorbit.json"
DEFAULT = {"version": 1, "roots": [".agents/skills"], "output": "SKILLS.md",
           "interval": 2, "project_rules": [], "descriptions": {}}
IGNORE = {".git", "node_modules", "__pycache__", ".venv", ".skillorbit"}


class OrbitError(Exception):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise OrbitError(f"Cannot read {path}: {exc}") from exc


def atomic_write(path, text):
    """Replace only changed files; preserve timestamps on an idle sync."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def write_json(path, data):
    return atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def inside(project, relative):
    path = (project / relative).resolve()
    if not path.is_relative_to(project.resolve()):
        raise OrbitError(f"Path must stay inside the project: {relative}")
    return path


def config_for(project):
    config = {**DEFAULT, **load_json(project / CONFIG, {})}
    if config["version"] != 1:
        raise OrbitError("Unsupported config version")
    roots = config["roots"]
    if not isinstance(roots, list) or not roots or any(not isinstance(r, str) for r in roots):
        raise OrbitError("roots must be a non-empty list of relative paths")
    for root in roots:
        inside(project, root)
    inside(project, config["output"])
    if float(config["interval"]) < 0.2:
        raise OrbitError("interval must be at least 0.2 seconds")
    for rule in config["project_rules"]:
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k), str) for k in ("match", "project")):
            raise OrbitError("Each project rule needs string match and project fields")
    return config


@contextlib.contextmanager
def sync_lock(project):
    """An OS advisory lock releases automatically after a crash."""
    folder = project / ".skillorbit"
    folder.mkdir(exist_ok=True)
    with (folder / "sync.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OrbitError("Another sync is active; try again shortly") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OrbitError("Another sync is active; try again shortly") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def skill_files(project, config):
    """Follow in-project links once, reject links escaping the project."""
    found, visited = [], set()

    def walk(folder):
        target = folder.resolve()
        if not target.is_relative_to(project) or target in visited:
            return
        visited.add(target)
        if not folder.exists():
            return
        if not folder.is_dir():
            raise OrbitError(f"Scan root is not a directory: {folder}")
        for item in sorted(folder.iterdir()):
            if item.name in IGNORE:
                continue
            if not item.resolve().is_relative_to(project):
                continue
            if item.is_dir():
                walk(item)
            elif item.name == "SKILL.md" and item.is_file():
                found.append(item)

    for root in config["roots"]:
        walk(inside(project, root))
    return sorted(set(found))


def frontmatter(path):
    text = path.read_text(encoding="utf-8-sig")
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match:
        raise OrbitError(f"Missing YAML frontmatter: {path}")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise OrbitError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OrbitError(f"Frontmatter must be a mapping: {path}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise OrbitError(f"Missing string name: {path}")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise OrbitError(f"Missing string description: {path}")
    return data, text


def first_sentence(text):
    text = re.sub(r"\s+", " ", text).strip()
    # Preserve decimals and abbreviations; never invent a role for new skills.
    match = re.search(r"[。！？]|(?<!\d)[.!?](?=\s+[A-Z]|$)", text)
    if match:
        text = text[:match.end()]
    if len(text) > 180:
        text = text[:177].rstrip() + "…"
    return text


def source_project(project, path, data, config):
    relative = path.relative_to(project).as_posix()
    for rule in config["project_rules"]:
        if fnmatch.fnmatchcase(relative, rule["match"]):
            return rule["project"]
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    explicit = data.get("project") or metadata.get("project") or metadata.get("upstream_suite")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for folder in (path.parent, *path.parents):
        if not folder.is_relative_to(project):
            break
        git_config = folder / ".git" / "config"
        if git_config.is_file():
            text = git_config.read_text(encoding="utf-8", errors="replace")
            remote = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*([^\n]+)', text)
            if remote:
                repo = re.search(r"[:/]([^/:\s]+/[^/\s]+?)(?:\.git)?$", remote.group(1).strip())
                if repo:
                    return repo.group(1)
        if folder == project:
            break
    return "未归属 / Unassigned"


def record_key(name, project):
    return json.dumps([project, name], ensure_ascii=False, separators=(",", ":"))


def scan(project, config):
    records = {}
    for path in skill_files(project, config):
        data, text = frontmatter(path)
        name = data["name"].strip()
        owner = source_project(project, path, data, config)
        key = record_key(name, owner)
        description = data["description"].strip()
        record = records.setdefault(key, {"key": key, "name": name, "project": owner,
                                         "description": "", "sources": []})
        record["sources"].append({"path": path.relative_to(project).as_posix(),
                                   "sha256": digest(text), "description": description})
    for record in records.values():
        # Shortest path prefers a primary install over a nested overlay.
        record["sources"].sort(key=lambda s: (len(Path(s["path"]).parts), s["path"]))
        description = record["sources"][0]["description"]
        override = config["descriptions"].get(record["name"])
        if isinstance(override, str):
            description = override
        elif isinstance(override, dict) and override.get("source_hash") == digest(description):
            description = override["text"]
        record["description"] = first_sentence(description)
    return sorted(records.values(), key=lambda r: (r["project"].casefold(), r["name"].casefold()))


def cells(line):
    if not line.strip().startswith("|"):
        return []
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def decode_cell(value):
    return html.unescape(re.sub(r"<br\s*/?>", "\n", value).replace(r"\|", "|"))


def table_rows(text):
    result = []
    for line in text.splitlines():
        parts = cells(line)
        if len(parts) < 3 or not (parts[0].startswith("`") and parts[0].endswith("`")):
            continue
        name = decode_cell(parts[0][1:-1])
        owner = decode_cell(parts[1])
        result.append((name, owner, decode_cell(parts[2]),
                       decode_cell(parts[3]) if len(parts) > 3 else ""))
    return result


def encode_cell(text):
    return html.escape(text, quote=False).replace("|", "&#124;").replace("\n", "<br>")


def managed_block(records, notes, output):
    count = sum(len(r["sources"]) for r in records)
    owners = len({r["project"] for r in records})
    lines = [BEGIN, "", f"**{len(records)} 个技能 · {count} 份定义 · {owners} 个归属项目**", "",
             "在备注列直接填写使用心得；同步时会自动保留。", "",
             "| Skill 名称 | 归属项目 | 一句话描述 | 备注 |", "|---|---|---|---|"]
    for r in records:
        name = encode_cell(r["name"]).replace("`", "&#96;")
        lines.append(f"| `{name}` | {encode_cell(r['project'])} | "
                     f"{encode_cell(r['description'])} | {encode_cell(notes.get(r['key'], ''))} |")
    lines.extend(["", "<details>", "<summary>展开完整来源索引 · 包括同名副本与变体</summary>", ""])
    for r in records:
        lines.append(f"- **{encode_cell(r['project'])} / {encode_cell(r['name'])}**")
        for source in r["sources"]:
            relative = os.path.relpath(source["path"], output.parent).replace(os.sep, "/")
            lines.append(f"  - [{encode_cell(source['path'])}]({quote(relative, safe='/')})")
    lines.extend(["", "</details>", "", END])
    return "\n".join(lines)


def reconcile(original, block):
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise OrbitError("Catalog needs exactly one begin/end marker pair")
        left = original.index(BEGIN)
        right = original.index(END)
        if left > right:
            raise OrbitError("Catalog markers are reversed")
        return original[:left] + block + original[right + len(END):]
    if original.strip():
        raise OrbitError("Output already exists without managed markers; use init --import-md or choose another output")
    return "# SkillOrbit · 技能星图\n\n项目技能随增删自动同步。直接在请求中指定技能名即可使用。\n\n" + block + "\n"


def sync(project, check=False, note=None):
    with sync_lock(project):
        config = config_for(project)
        records = scan(project, config)  # All inputs must be valid before any output changes.
        output = inside(project, config["output"])
        original = output.read_text(encoding="utf-8") if output.exists() else ""
        state_path = project / ".skillorbit" / "state.json"
        state = load_json(state_path, {"version": 1, "notes": {}, "records": [], "changes": []})
        notes = dict(state["notes"])
        if BEGIN in original and END in original:
            table = original.split(BEGIN, 1)[1].split(END, 1)[0]
            for name, owner, _, memo in table_rows(table):
                key = record_key(name, owner)
                if memo or key in notes:
                    notes[key] = memo
        if note:
            name, memo, owner = note
            candidates = [r for r in records if r["name"] == name and (not owner or r["project"] == owner)]
            if len(candidates) != 1:
                raise OrbitError(f"Expected one skill named {name!r}; found {len(candidates)}. Use --owner to disambiguate")
            notes[candidates[0]["key"]] = memo
        # Source paths are relative to the project in state and catalog links.
        block = managed_block(records, notes, Path(config["output"]))
        generated = reconcile(original, block)
        changed = generated != original
        old = {r["key"]: r for r in state["records"]}
        new = {r["key"]: r for r in records}
        added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
        updated = sorted(k for k in new.keys() & old.keys() if new[k] != old[k])
        if check:
            print(f"{'STALE' if changed else 'CURRENT'} · {len(records)} skills · "
                  f"{sum(len(r['sources']) for r in records)} files")
            return 1 if changed else 0
        if (output.read_text(encoding="utf-8") if output.exists() else "") != original:
            raise OrbitError("Catalog changed during sync; retry to preserve the new edits")
        atomic_write(output, generated)
        changes = state.get("changes", [])
        if added or removed or updated:
            changes = (changes + [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "added": added, "removed": removed, "updated": updated}])[-100:]
        write_json(state_path, {"version": 1, "notes": notes, "records": records, "changes": changes})
        print(f"{'SYNCED' if changed else 'CURRENT'} · {len(records)} skills · "
              f"+{len(added)} −{len(removed)} ~{len(updated)} · {config['output']}", flush=True)
        return 0


def init(project, roots=None, output=None, import_md=None):
    config_path = project / CONFIG
    if config_path.exists():
        raise OrbitError("Already initialized; edit .skillorbit.json or run sync")
    config = {**DEFAULT, "roots": roots or DEFAULT["roots"], "output": output or DEFAULT["output"],
              "project_rules": [], "descriptions": {}}
    legacy = None
    if import_md:
        legacy = inside(project, import_md)
        legacy_text = legacy.read_text(encoding="utf-8")
        rows = table_rows(legacy_text)
        if not rows:
            raise OrbitError("Import did not find a skill table; expected backtick-quoted names")
        for name, owner, description, _ in rows:
            owner = re.sub(r"（含.*?）$|（(?:Codex|Claude|Gemini).*?）$", "", owner).strip()
            config["project_rules"].append({"match": f"*/{name}/SKILL.md", "project": owner})
        # Name may differ from its containing folder (researchwrite is one example).
        for path in skill_files(project, config):
            data, _ = frontmatter(path)
            for name, owner, description, _ in rows:
                if data["name"] == name and path.parent.name != name:
                    owner = re.sub(r"（含.*?）$|（(?:Codex|Claude|Gemini).*?）$", "", owner).strip()
                    config["project_rules"].insert(0, {"match": path.relative_to(project).as_posix(), "project": owner})
        records = scan(project, config)
        for name, _, description, _ in rows:
            match = next((r for r in records if r["name"] == name), None)
            if match:
                config["descriptions"][name] = {"text": description,
                    "source_hash": digest(match["sources"][0]["description"])}
    # Check target before writing configuration or replacing a legacy catalog.
    target = inside(project, config["output"])
    if target.exists() and (not legacy or target != legacy):
        raise OrbitError(f"Output exists: {target}. Choose a new --output")
    config["project_rules"] = list({(r["match"], r["project"]): r for r in config["project_rules"]}.values())
    scan(project, config)
    if legacy and target == legacy:
        backup = target.with_name(target.name + ".pre-skillorbit.bak")
        if backup.exists():
            raise OrbitError(f"Backup already exists: {backup}")
        atomic_write(backup, legacy_text)
        atomic_write(target, "")
    write_json(config_path, config)
    if legacy:
        notes = {}
        for name, owner, _, memo in rows:
            owner = re.sub(r"（含.*?）$|（(?:Codex|Claude|Gemini).*?）$", "", owner).strip()
            notes[record_key(name, owner)] = memo
        write_json(project / ".skillorbit" / "state.json",
                   {"version": 1, "notes": notes, "records": [], "changes": []})
    return sync(project)


def fingerprint(project):
    config = config_for(project)
    paths = skill_files(project, config) + [project / CONFIG, inside(project, config["output"])]
    result = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
    return result


def watch(project, interval=None, token=None, managed=False):
    interval = interval or float(config_for(project)["interval"])
    if interval < 0.2:
        raise OrbitError("interval must be at least 0.2 seconds")
    stop_path = project / ".skillorbit" / "stop"
    runtime_path = project / ".skillorbit" / "runtime.json"
    if managed:
        with sync_lock(project):
            if running(project):
                raise OrbitError("Another background watcher is already active")
            stop_path.unlink(missing_ok=True)
            token = uuid.uuid4().hex
            write_json(runtime_path, {"pid": os.getpid(), "token": token,
                       "heartbeat": time.time(), "interval": interval})
    exiting = False

    def request_stop(*_):
        nonlocal exiting
        exiting = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    last, previous_error = None, None
    print(f"WATCHING · {project} · every {interval:g}s", flush=True)
    try:
        while not exiting:
            if token:
                runtime = load_json(runtime_path, {})
                if runtime.get("token") != token or stop_path.exists():
                    break
                runtime["heartbeat"] = time.time()
                runtime["ready"] = True
                write_json(runtime_path, runtime)
            try:
                current = fingerprint(project)
                if current != last:
                    sync(project)
                    last = fingerprint(project)
                previous_error = None
            except (OrbitError, OSError, ValueError) as exc:
                if str(exc) != previous_error:
                    print(f"WAITING · {exc}", file=sys.stderr, flush=True)
                previous_error = str(exc)
            # Signal handler can interrupt short waits; no background busy loop.
            time.sleep(interval)
    finally:
        if token and load_json(runtime_path, {}).get("token") == token:
            runtime_path.unlink(missing_ok=True)
            stop_path.unlink(missing_ok=True)
    return 0


def running(project):
    runtime = load_json(project / ".skillorbit" / "runtime.json", {})
    if not runtime:
        return None
    if time.time() - runtime.get("heartbeat", 0) > max(15, runtime.get("interval", 2) * 5):
        return None
    try:
        os.kill(runtime["pid"], 0)
    except (ProcessLookupError, KeyError):
        return None
    except PermissionError:
        # Sandboxed hosts may deny a PID probe even when the watcher is alive.
        # The fresh project-owned heartbeat still provides evidence of liveness.
        pass
    return runtime


def start(project):
    with sync_lock(project):
        if running(project):
            print("Already watching")
            return 0
        folder = project / ".skillorbit"
        (folder / "stop").unlink(missing_ok=True)
        token = uuid.uuid4().hex
        interval = float(config_for(project)["interval"])
        write_json(folder / "runtime.json", {"pid": 0, "token": token,
                   "heartbeat": time.time(), "interval": interval})
        env = os.environ.copy()
        if env.get("PYTHONPATH"):
            env["PYTHONPATH"] = os.pathsep.join(str(Path(p or ".").resolve())
                                                for p in env["PYTHONPATH"].split(os.pathsep))
        # Works both from a package install and an explicitly supplied source PYTHONPATH.
        command = [sys.executable, "-m", "skillorbit", "--project", str(project), "watch", "--token", token]
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        with (folder / "watch.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=env, cwd=project, **options)
        threading.Thread(target=process.wait, daemon=True).start()
        write_json(folder / "runtime.json", {"pid": process.pid, "token": token,
                   "heartbeat": time.time(), "interval": interval})
    # Let the child observe the actual PID before it writes a heartbeat.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OrbitError(f"Watcher exited; inspect {folder / 'watch.log'}")
        runtime = load_json(folder / "runtime.json", {})
        if runtime.get("ready") and running(project):
            print(f"STARTED · pid {process.pid} · logs: .skillorbit/watch.log")
            return 0
        time.sleep(0.1)
    raise OrbitError("Watcher did not start; inspect .skillorbit/watch.log")


def stop(project):
    runtime = running(project)
    if not runtime:
        print("Not watching")
        return 0
    atomic_write(project / ".skillorbit" / "stop", runtime["token"])
    deadline = time.monotonic() + max(6, runtime["interval"] + 3)
    while time.monotonic() < deadline:
        current = load_json(project / ".skillorbit" / "runtime.json", {})
        if not current or current.get("token") != runtime["token"]:
            print("STOPPED")
            return 0
        time.sleep(0.1)
    raise OrbitError("Stop requested; watcher may be finishing a scan. Inspect .skillorbit/watch.log")


def service(project, action):
    """Native optional macOS login startup, scoped to this one project."""
    if sys.platform != "darwin":
        raise OrbitError("Login service is available on macOS. Use start or watch on this platform")
    label = "io.skillorbit." + digest(str(project))[:12]
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    if action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        target.unlink(missing_ok=True)
        print("LOGIN SERVICE REMOVED")
        return 0
    config_for(project)
    stop(project)
    folder = project / ".skillorbit"
    folder.mkdir(exist_ok=True)
    specification = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "skillorbit", "--project", str(project),
                             "watch", "--managed"],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": str(folder / "watch.log"),
        "StandardErrorPath": str(folder / "watch.log"),
    }
    if os.environ.get("PYTHONPATH"):
        specification["EnvironmentVariables"] = {"PYTHONPATH": os.pathsep.join(
            str(Path(p or ".").resolve()) for p in os.environ["PYTHONPATH"].split(os.pathsep))}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    atomic_write(target, plistlib.dumps(specification).decode("utf-8"))
    result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
    if result.returncode:
        raise OrbitError(f"Login service was written but launchctl failed: {result.stderr.strip()}")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        runtime = running(project)
        if runtime and runtime.get("ready"):
            print(f"LOGIN SERVICE INSTALLED · pid {runtime['pid']} · starts at login")
            return 0
        time.sleep(0.2)
    raise OrbitError("Service installed but not ready yet; inspect .skillorbit/watch.log")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skillorbit", description="A living Markdown directory for your project's AI skills.")
    parser.add_argument("--version", action="version", version=f"SkillOrbit {__version__}")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="Initialize the project catalog")
    init_parser.add_argument("--root", action="append", help="Scan root, repeat for multiple roots")
    init_parser.add_argument("--output", help="Catalog path relative to project")
    init_parser.add_argument("--import-md", help="Import descriptions, owners and notes from an old catalog")
    sync_parser = commands.add_parser("sync", help="Reconcile the catalog once")
    sync_parser.add_argument("--check", action="store_true", help="Exit 1 if the catalog is stale; do not update it")
    watch_parser = commands.add_parser("watch", help="Watch in the foreground")
    watch_parser.add_argument("--interval", type=float)
    watch_parser.add_argument("--token", help=argparse.SUPPRESS)
    watch_parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("start", help="Start a local background watcher")
    commands.add_parser("stop", help="Stop the background watcher")
    commands.add_parser("status", help="Show background watcher status")
    service_parser = commands.add_parser("service", help="Install or remove macOS login startup")
    service_parser.add_argument("action", choices=["install", "uninstall"])
    note_parser = commands.add_parser("note", help="Set a remark without opening Markdown")
    note_parser.add_argument("name")
    note_parser.add_argument("text")
    note_parser.add_argument("--owner")
    list_parser = commands.add_parser("list", help="Search the current inventory")
    list_parser.add_argument("query", nargs="?", default="")
    list_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    try:
        if not project.is_dir():
            raise OrbitError(f"Project does not exist: {project}")
        if args.command == "init":
            return init(project, args.root, args.output, args.import_md)
        if args.command == "sync":
            return sync(project, args.check)
        if args.command == "watch":
            return watch(project, args.interval, args.token, args.managed)
        if args.command == "start":
            return start(project)
        if args.command == "stop":
            return stop(project)
        if args.command == "status":
            runtime = running(project)
            print(f"WATCHING · pid {runtime['pid']}" if runtime else "STOPPED")
            return 0 if runtime else 1
        if args.command == "service":
            return service(project, args.action)
        if args.command == "note":
            return sync(project, note=(args.name, args.text, args.owner))
        if args.command == "list":
            records = scan(project, config_for(project))
            query = args.query.casefold()
            records = [r for r in records if query in (r["name"] + " " + r["project"] + " " + r["description"]).casefold()]
            if args.json:
                print(json.dumps(records, ensure_ascii=False, indent=2))
            else:
                for r in records:
                    print(f"{r['name']}  [{r['project']}]  {r['description']}")
            return 0
    except (OrbitError, OSError, ValueError, TypeError) as exc:
        print(f"SkillOrbit: {exc}", file=sys.stderr)
        return 2
    return 0
