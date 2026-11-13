"""A fake ``hermes`` executable for the profile writer tests.

Installed on PATH by tests/test_profile_writer.py through a tiny wrapper that sets
SHIM_LOG (JSON lines, one argv per call) and SHIM_CONTROL (JSON the test edits):

  {"fail_match": "config set",   # fail any call whose argv text contains this
   "fail_stderr": "...",         # stderr to print on that failure (may echo a secret)
   "fail_stdout": "",
   "fail_code": 1,
   "cancel_install": false,      # print "Install cancelled." and exit 0, create nothing
   "listed": ["name"]}           # extra names reported by `profile list`

   "import_fail_after_create": false,  # profile import: make the directory, then exit 1

Implements: --version, profile install <dir> --name N -y, profile delete N -y,
profile update N -y, profile list, -p N config set KEY VALUE, profile export N -o FILE,
profile import FILE --name N. Output mimics T0 and the 0.23 export/import behavior.
It never reads or writes anything outside HERMES_HOME, except the import wrapper under HOME (like Hermes).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

import yaml


def main(argv: list[str]) -> int:
    log = os.environ.get("SHIM_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    control: dict = {}
    cpath = os.environ.get("SHIM_CONTROL")
    if cpath and Path(cpath).exists():
        control = json.loads(Path(cpath).read_text())
    home = Path(os.environ["HERMES_HOME"])
    text = " ".join(argv)

    match = control.get("fail_match")
    if match and match in text:
        sys.stdout.write(control.get("fail_stdout", ""))
        sys.stderr.write(control.get("fail_stderr", ""))
        return int(control.get("fail_code", 1))

    if argv[:1] == ["--version"]:
        print("Hermes Agent v0.23.1 (2026.9.24) · stable · local shim")
        print("Install method: git")
        return 0

    if argv[:2] == ["profile", "install"]:
        src = Path(argv[2])
        name = argv[argv.index("--name") + 1]
        if "-y" not in argv:
            print("Install cancelled.")
            return 0
        if control.get("cancel_install"):
            print("Install cancelled.")
            return 0
        target = home / "profiles" / name
        if target.exists():
            print(f"Error: Profile '{name}' already exists at {target}. Use `hermes profile update` "
                  "to upgrade in place, or pass --force to overwrite.")
            return 1
        shutil.copytree(src, target)
        (target / "memories").mkdir(exist_ok=True)
        if not (target / "config.yaml").exists():
            (target / "config.yaml").write_text("model: {}\n")
        print(f"\nDistribution: shim v0.1.0\n  Target:   {target}\n\n"
              f"✓ Installed '{name}' v0.1.0\n  Profile path: {target}\n\n"
              f"  Use with:      hermes -p {name} chat")
        return 0

    if argv[:2] == ["profile", "delete"]:
        name = argv[2]
        target = home / "profiles" / name
        if not target.is_dir():
            print(f"Error: Profile '{name}' does not exist.")
            return 1
        shutil.rmtree(target)
        if os.environ.get("HOME"):
            wrapper = Path(os.environ["HOME"]) / ".local" / "bin" / name
            if wrapper.is_file() and f"hermes -p {name} " in wrapper.read_text():
                wrapper.unlink()      # like Hermes: only its own wrapper, never a user's file
        print(f"✓ Deleted profile '{name}'")
        return 0

    if argv[:2] == ["profile", "export"]:
        # Like Hermes 0.23: a .tar.gz of the profile dir under its own name, without .env and auth.json
        # at the profile root (everything else, including token*, *.pem and the marker, is kept).
        name, out = argv[2], Path(argv[argv.index("-o") + 1])
        target = home / "profiles" / name
        if not target.is_dir():
            print(f"Error: Profile '{name}' does not exist.")
            return 1

        def skip(info):
            rel = info.name.split("/", 1)[1] if "/" in info.name else ""
            return None if rel in (".env", "auth.json") else info
        with tarfile.open(out, "w:gz") as tar:
            tar.add(target, arcname=name, filter=skip)
        print(f"✓ Exported '{name}' to {out}")
        return 0

    if argv[:2] == ["profile", "import"]:
        archive, name = Path(argv[2]), argv[argv.index("--name") + 1]
        target = home / "profiles" / name
        if target.exists():
            print(f"Error: A profile named '{name}' already exists.")
            return 1
        stage = home / f".import-{name}"
        with tarfile.open(archive, "r:gz") as tar:
            top = tar.getmembers()[0].name.split("/")[0]
            tar.extractall(stage, filter="data")
        shutil.move(str(stage / top), str(target))
        shutil.rmtree(stage)
        if control.get("import_fail_after_create"):
            print("Error: something broke after the directory was made")
            return 1
        wrapper = None
        if os.environ.get("HOME"):
            wrapper = Path(os.environ["HOME"]) / ".local" / "bin" / name
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(f"#!/bin/sh\nexec hermes -p {name} \"$@\"\n")
        print(f"✓ Imported profile '{name}' at {target}")
        if wrapper:
            print(f"  Wrapper created: {wrapper}")
        return 0

    if argv[:2] == ["profile", "update"]:
        print(f"✓ Updated '{argv[2]}' → v0.1.0")
        return 0

    if argv[:2] == ["profile", "list"]:
        print(" Profile          Model        Gateway      Alias        Distribution")
        print(" ───────────────    ──────────    ───────    ───────")
        print(" ◆default         -            stopped      -            -")
        names = sorted(p.name for p in (home / "profiles").glob("*") if p.is_dir())
        for n in list(names) + [x for x in control.get("listed", []) if x not in names]:
            print(f"  {n:<15}-            stopped      -            -")
        return 0

    if argv[:1] == ["-p"] and argv[2:4] == ["config", "set"]:
        name, key, value = argv[1], argv[4], argv[5]
        cfg_path = home / "profiles" / name / "config.yaml"
        if not cfg_path.is_file():
            print(f"Error: no such profile '{name}'")
            return 1
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        node = cfg
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = {"true": True, "false": False}.get(value.lower(), value)
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        print(f"✓ Set {key} = {node[parts[-1]]} in {cfg_path}")
        return 0

    print("shim: unsupported call", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
