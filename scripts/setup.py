#!/usr/bin/env python3
"""Phoenix setup: inspect includes, preview, back up, configure and verify."""
import argparse
import difflib
import fnmatch
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klippy.extras.phoenix_core import atomic_write

HEADER = re.compile(r"^\[([^]\r\n]+)\][ \t]*(?:[#;].*)?$", re.M)
AUTOSAVE = "#*# <---------------------- SAVE_CONFIG ---------------------->"
MODULES = ("phoenix.py", "phoenix_core.py")
SSH_ORIGIN = "git@github.com:ozancs/phoenix.git"
HTTPS_ORIGIN = "https://github.com/ozancs/phoenix.git"


class ConfigTree:
    def __init__(self, main):
        self.main = Path(main).resolve()
        self.root = self.main.parent
        self.original, self.text, self.includes = {}, {}, []
        self._load(self.main, [])

    def _load(self, path, stack):
        path = path.resolve()
        if path in stack:
            raise ValueError("Include cycle: " + str(path))
        if path in self.text:
            raise ValueError("Config included more than once: " + str(path))
        data = path.read_bytes().decode("utf-8")
        self.original[path] = data
        self.text[path] = data.replace("\r\n", "\n")
        for match in HEADER.finditer(self.text[path].split(AUTOSAVE)[0]):
            name = match[1].strip()
            if name.lower().startswith("include "):
                pattern = str(path.parent / name[8:].strip())
                self.includes.append(pattern)
                matches = sorted(glob.glob(pattern))
                if not matches and not glob.has_magic(pattern):
                    raise ValueError("Missing included config: " + pattern)
                for child in matches:
                    self._load(Path(child), stack + [path])

    def sections(self, name):
        found = []
        for path, data in self.text.items():
            body = data.split(AUTOSAVE)[0]
            headers = list(HEADER.finditer(body))
            for index, match in enumerate(headers):
                if match[1].strip().lower() == name.lower():
                    end = headers[index+1].start() if index+1 < len(headers) else len(body)
                    found.append((path, match.end(), end))
        return found

    def one(self, name):
        matches = self.sections(name)
        if len(matches) > 1:
            raise ValueError("Duplicate [%s] sections; resolve before setup" % name)
        return matches[0] if matches else None

    def append(self, path, text):
        path = Path(path).resolve()
        data = self.text.get(path, "")
        marker = data.find(AUTOSAVE)
        index = marker if marker >= 0 else len(data)
        self.text[path] = data[:index].rstrip() + "\n\n" + text.rstrip() + "\n\n" + data[index:]

    def set_option(self, section, option, value):
        path, start, end = self.one(section)
        block = self.text[path][start:end]
        matches = list(re.finditer(r"^([ \t]*)" + re.escape(option) + r"\s*[:=][^\n]*$", block, re.M))
        if len(matches) > 1:
            raise ValueError("Duplicate option: [%s] %s" % (section, option))
        if matches:
            match = matches[0]
            old_value = re.split(r"[:=]", match[0], maxsplit=1)[1]
            if re.split(r"[#;]", old_value)[0].strip().lower() == str(value).lower():
                return
            comment = re.search(r"\s+[#;].*$", old_value)
            replacement = "%s%s: %s%s" % (match[1], option, value, comment[0] if comment else "")
            block = block[:match.start()] + replacement + block[match.end():]
        else:
            block = "\n%s: %s\n" % (option, value) + block.lstrip("\n")
        self.text[path] = self.text[path][:start] + block + self.text[path][end:]

    def hook(self, macro, command, at_start):
        path, start, end = self.one("gcode_macro " + macro)
        block = self.text[path][start:end]
        if re.search(r"^\s*" + command + r"(?:\s|$)", block, re.M):
            return False
        lines = block.splitlines(keepends=True)
        options = [i for i, line in enumerate(lines) if re.match(r"^\s*gcode\s*:", line)]
        if len(options) != 1:
            raise ValueError("Cannot safely find gcode body in " + macro)
        index = options[0]
        if lines[index].split(":", 1)[1].strip():
            raise ValueError("Inline gcode requires manual review in " + macro)
        base = len(lines[index]) - len(lines[index].lstrip())
        stop, indent = index + 1, None
        while stop < len(lines):
            line = lines[stop]
            if line.strip() and not line.lstrip().startswith(("#", ";")):
                width = len(line) - len(line.lstrip())
                if width <= base:
                    break
                indent = min(indent, width) if indent is not None else width
            stop += 1
        insertion = index + 1 if at_start else stop
        if insertion > 0 and not lines[insertion-1].endswith("\n"):
            lines[insertion-1] += "\n"
        lines.insert(insertion, " " * (indent or base + 4) + command + " # Phoenix print hook\n")
        self.text[path] = self.text[path][:start] + "".join(lines) + self.text[path][end:]
        return True

    def changes(self):
        result = {}
        for path, content in self.text.items():
            old = self.original.get(path)
            if old is None or old.replace("\r\n", "\n") != content:
                path.relative_to(self.root)  # Do not rewrite shared files outside this instance.
                result[path] = content.encode("utf-8")
        return result


def plan_config(config_dir, repo=ROOT, origin=HTTPS_ORIGIN, start_macro=None, end_macro=None):
    config_dir = Path(config_dir).resolve()
    tree = ConfigTree(config_dir / "printer.cfg")
    notes, phoenix = [], config_dir / "phoenix.cfg"
    if not tree.one("phoenix"):
        if phoenix.exists() and phoenix not in tree.text:
            extra = ConfigTree(phoenix)
            if not extra.one("phoenix"):
                raise ValueError("Existing phoenix.cfg has no [phoenix] section")
            tree.original.update(extra.original)
            tree.text.update(extra.text)
        elif not phoenix.exists():
            tree.text[phoenix] = (repo / "config/phoenix.cfg").read_text(encoding="utf-8").replace(
                "state_path: ~/printer_data/phoenix", "state_path: " + (config_dir.parent / "phoenix").as_posix())
        if not any(fnmatch.fnmatch(str(phoenix), pattern) for pattern in tree.includes):
            tree.append(tree.main, "[include phoenix.cfg]")
        if not tree.one("phoenix"):
            raise ValueError("Phoenix section could not be installed")
    if tree.one("force_move"):
        tree.set_option("force_move", "enable_force_move", "True")
    else:
        tree.append(phoenix if phoenix in tree.text else tree.main, "[force_move]\nenable_force_move: True")
    for text in tree.text.values():
        if re.search(r"#\*#\s*\[force_move\]", text.partition(AUTOSAVE)[2]):
            raise ValueError("Autosaved [force_move] requires manual review; no changes applied")
    for command, chosen, candidates, first in (
            ("PHOENIX_START", start_macro, ("PRINT_START", "START_PRINT"), False),
            ("PHOENIX_END", end_macro, ("PRINT_END", "END_PRINT"), True)):
        names = [chosen] if chosen else [name for name in candidates if tree.one("gcode_macro " + name)]
        if len(names) == 1 and tree.one("gcode_macro " + names[0]):
            added = tree.hook(names[0], command, first)
            notes.append(("ADD " if added else "OK  ") + command + " in " + names[0])
        elif chosen:
            raise ValueError("Requested macro not found: " + chosen)
        else:
            notes.append("TODO " + command + ": " + ("ambiguous macros; choose --start-macro/--end-macro"
                         if names else "no standard macro; add this command to slicer start/end code"))
    if not tree.one("gcode_macro PHOENIX_SETUP_GUIDE"):
        tree.append(phoenix if phoenix in tree.text else tree.main,
                    "[gcode_macro PHOENIX_SETUP_GUIDE]\ndescription: Open Phoenix setup\ngcode:\n    PHOENIX_SETUP")
    changes = tree.changes()
    originals = {path: tree.original.get(path) for path in changes}
    moonfile = config_dir / "moonraker.conf"
    if moonfile.exists():
        moon = ConfigTree(moonfile)
        if not moon.one("update_manager phoenix"):
            moon.append(moonfile, "[update_manager phoenix]\ntype: git_repo\nchannel: dev\n"
                        "path: %s\norigin: %s\nprimary_branch: main\nmanaged_services: klipper\n"
                        % (repo.resolve().as_posix(), origin))
        else:
            for key, value in {"type": "git_repo", "path": repo.resolve().as_posix(), "origin": origin,
                               "channel": "dev", "primary_branch": "main", "managed_services": "klipper"}.items():
                moon.set_option("update_manager phoenix", key, value)
        for path, content in moon.changes().items():
            if path in changes:
                raise ValueError("Printer and Moonraker share an edited file: " + str(path))
            changes[path], originals[path] = content, moon.original.get(path)
        notes.append("OK  Moonraker Update Manager entry planned/installed")
    else:
        notes.append("TODO moonraker.conf not found; Update Manager not configured")
    return changes, originals, notes


def apply_changes(changes, originals, backup_root):
    for path in changes:
        actual = path.read_bytes().decode("utf-8") if path.exists() else None
        if actual != originals[path]:
            raise ValueError("Config changed during setup; rerun: " + str(path))
    if not changes:
        return None
    backup = Path(backup_root) / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    manifest, applied, modes = [], [], {}
    for index, path in enumerate(changes):
        entry = {"path": str(path), "backup": None}
        if path.exists():
            modes[path] = path.stat().st_mode & 0o777
            saved = backup / (str(index) + "-" + path.name + ".bak")
            shutil.copy2(str(path), str(saved))
            entry["backup"] = str(saved)
        manifest.append(entry)
    atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
    try:
        for path, data in changes.items():
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            atomic_write(path, data)
            applied.append(path)
            os.chmod(path, mode)
    except Exception:
        for path in reversed(applied):
            if originals[path] is None:
                path.unlink()
            else:
                atomic_write(path, originals[path].encode("utf-8"))
                os.chmod(path, modes[path])
        raise
    return backup


def moonraker_call(base, path, payload=None):
    headers = {}
    if os.environ.get("MOONRAKER_API_KEY"):
        headers["X-Api-Key"] = os.environ["MOONRAKER_API_KEY"]
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["result"]


def printer_state(base):
    try:
        status = moonraker_call(base, "/printer/objects/query?print_stats&virtual_sdcard")["status"]
        return "printing" if status.get("virtual_sdcard", {}).get("is_active") else status.get("print_stats", {}).get("state", "unknown")
    except Exception:
        return "unknown"


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30).stdout.strip()


def deploy_command(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("Deploy key not found: " + str(path))
    return "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes" % shlex.quote(str(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default=os.environ.get("CONFIG_DIR", "~/printer_data/config"))
    parser.add_argument("--klipper-dir", default=os.environ.get("KLIPPER_DIR", "~/klipper"))
    parser.add_argument("--moonraker-url", default=os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125"))
    parser.add_argument("--check", action="store_true", help="Show plan; change nothing")
    parser.add_argument("--yes", action="store_true", help="Apply displayed config plan without prompting")
    parser.add_argument("--restart", action="store_true", help="Restart via Moonraker only if confirmed idle")
    parser.add_argument("--start-macro")
    parser.add_argument("--end-macro")
    parser.add_argument("--deploy-key", help="Existing printer-side SSH key path (never read/printed)")
    args = parser.parse_args(argv)
    config_dir, klipper_dir = (Path(value).expanduser().resolve() for value in (args.config_dir, args.klipper_dir))
    if not (klipper_dir / "klippy/extras").is_dir():
        raise ValueError("Klipper not found; use --klipper-dir")
    links = []
    for module in MODULES:
        source, target = ROOT / "klippy/extras" / module, klipper_dir / "klippy/extras" / module
        if target.is_symlink() and target.resolve() == source.resolve():
            continue
        if target.exists() or target.is_symlink():
            raise ValueError("Refusing to replace unrelated module: " + str(target))
        links.append((source, target))
    origin = git(ROOT, "remote", "get-url", "origin")
    if origin not in (HTTPS_ORIGIN, HTTPS_ORIGIN[:-4], SSH_ORIGIN):
        raise ValueError("Unexpected origin. Review repository before enabling its updater.")
    ssh_command = deploy_command(args.deploy_key) if args.deploy_key else None
    if ssh_command:
        origin = SSH_ORIGIN
    changes, originals, notes = plan_config(config_dir, ROOT, origin, args.start_macro, args.end_macro)
    print("\nPhoenix setup — inspect / configure / verify\n")
    for _, target in links:
        print("ADD module link: " + str(target))
    for path, content in changes.items():
        print("".join(difflib.unified_diff((originals[path] or "").splitlines(True),
              content.decode().splitlines(True), fromfile=str(path), tofile=str(path))), end="")
    for note in notes:
        print(note)
    if ssh_command:
        print("SSH: supplied deploy key will be used for this repo only; read-only access is enough.")
    if not changes and not links:
        print("OK  Configuration and module links already present.")
    if args.check:
        print("Check only. Nothing changed.")
        return 0
    if printer_state(args.moonraker_url) in ("printing", "paused"):
        raise ValueError("A print is running/paused. Rerun setup when finished.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise ValueError("Use --check to inspect or --yes to apply non-interactively")
        if input("Apply this plan with backups? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 0
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    elif origin == SSH_ORIGIN:
        try:
            inherited = git(ROOT, "config", "--get", "core.sshCommand")
        except subprocess.CalledProcessError:
            inherited = "ssh"
        env["GIT_SSH_COMMAND"] = inherited + " -o BatchMode=yes -o StrictHostKeyChecking=yes"
    try:
        git(ROOT, "ls-remote", origin, "HEAD", env=env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("Repo access failed. Add the public deploy key on GitHub, verify GitHub's "
                         "SSH host key, then rerun with --deploy-key. No config changed.")
    if printer_state(args.moonraker_url) in ("printing", "paused"):
        raise ValueError("Printing started; no setup changes applied")
    backup = apply_changes(changes, originals, config_dir.parent / "phoenix-setup-backups")
    for source, target in links:
        target.symlink_to(source)
    exclude = klipper_dir / ".git/info/exclude"
    if exclude.is_file():
        text = exclude.read_text()
        additions = ["klippy/extras/" + module for module in MODULES if "klippy/extras/" + module not in text.splitlines()]
        if additions:
            atomic_write(exclude, (text.rstrip() + "\n" + "\n".join(additions) + "\n").encode())
    if ssh_command:
        git(ROOT, "config", "core.sshCommand", ssh_command)
        git(ROOT, "remote", "set-url", "origin", SSH_ORIGIN)
    if backup:
        print("Backup: " + str(backup))
    print("Installed. No homing, heating or printer movement performed.")
    do_restart = args.restart
    if not do_restart and sys.stdin.isatty() and not args.yes:
        do_restart = input("Load setup now (restart Klipper and Moonraker if idle)? [Y/n] ").strip().lower() in ("", "y", "yes")
    if do_restart:
        if printer_state(args.moonraker_url) not in ("standby", "complete", "cancelled"):
            print("Restart deferred: cannot confirm idle. Restart from your UI when idle.")
        else:
            moonraker_call(args.moonraker_url, "/printer/gcode/script", {"script": "RESTART"})
            try:
                moonraker_call(args.moonraker_url, "/machine/services/restart", {"service": "moonraker"})
            except Exception:
                print("Moonraker restart response unavailable; reconnect and verify the updater.")
    print("After reconnect: PHOENIX_SETUP_GUIDE (or PHOENIX_SETUP) in the UI.")
    print("Guide checks print hooks and asks you to review the park point. No Z homing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("Phoenix setup stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
