"""Compile SteamTracking's Deadlock .proto dump into src/ailock/_proto.

The dump references Valve-internal options (boxed_type, additional_includes, ...)
that are never defined, so we strip them into a temp copy first, and compile only
the files we decode (the full dump has duplicate definitions across Steam protos).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import grpc_tools

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "vendor/Protobufs/deadlock"
TMP = ROOT / "build/proto"
OUT = ROOT / "src/ailock/_proto"
# match metadata, demo container + packets, game/user messages
ROOTS = [
    "citadel_gcmessages_common.proto", "demo.proto", "netmessages.proto", "networkbasetypes.proto",
    "usermessages.proto", "citadel_usermessages.proto", "gameevents.proto", "citadel_gameevents.proto",
    "te.proto", "base_modifier.proto",
]


def flat(name: str) -> str:
    # dotted names (foo.steamworkssdk.proto) would become python packages; flatten them
    return name.replace(".steamworkssdk.proto", "_steamworkssdk.proto")


def clean(text: str, opts: set[str]) -> str:
    names = "|".join(sorted(opts))
    text = re.sub(r"^\s*option\s+(%s)\s*=.*;\s*$" % names, "", text, flags=re.M)
    # drop `name = value` entries for undefined options inside [...] field option lists
    text = re.sub(r'\s*\b(%s)\s*=\s*("[^"]*"|[\w.]+)\s*,?' % names, "", text)
    text = re.sub(r",\s*\]", "]", text)
    text = re.sub(r"\s*\[\s*\]", "", text)
    return flat(text)


def closure(roots: list[str]) -> list[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name.startswith("google/"):
            continue
        seen.add(name)
        stack += re.findall(r'^import "([^"]+)";', (SRC / name).read_text(), flags=re.M)
    return sorted(flat(n) for n in seen)


def compile_once(opts: set[str]) -> subprocess.CompletedProcess:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    for p in SRC.glob("*.proto"):
        (TMP / flat(p.name)).write_text(clean(p.read_text(), opts))
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    (OUT / "__init__.py").write_text("")
    builtin = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")
    return subprocess.run(
        [sys.executable, "-m", "grpc_tools.protoc", f"-I{builtin}", f"-I{TMP}", f"--python_out={OUT}",
         *closure(ROOTS)],
        capture_output=True, text=True,
    )


def main() -> None:
    # options the dump uses but never defines; more are discovered from protoc errors
    opts = {"boxed_type", "additional_includes"}
    while True:
        r = compile_once(opts)
        unknown = set(re.findall(r'Option "(\w+)" unknown', r.stderr))
        if r.returncode == 0 or not unknown - opts:
            break
        opts |= unknown
    if r.returncode:
        print("\n".join(l for l in r.stderr.splitlines() if ".proto:" in l and "warning:" not in l)[:4000])
        sys.exit(r.returncode)
    # generated modules use top-level imports (`import foo_pb2`); make them package-relative
    for f in OUT.glob("*_pb2.py"):
        f.write_text(re.sub(r"^import (\w+_pb2) as", r"from . import \1 as", f.read_text(), flags=re.M))
    print(f"compiled protos -> {OUT.relative_to(ROOT)} (stripped options: {', '.join(sorted(opts))})")


if __name__ == "__main__":
    main()
