#!/usr/bin/env python3

from __future__ import annotations

import argparse
import atexit
import os
import select
import shutil
import subprocess
import sys
import tarfile
import time
from collections import namedtuple
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"

RESET = "\033[0m"
DIM = "\033[2m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

TIPS = (
    f"{DIM}↑/↓{RESET} to navigate"
    f" • {DIM}Space:{RESET} select"
    f" • {DIM}Enter:{RESET} confirm"
    f" • {DIM}Type:{RESET} to search"
)

CONTROL_KEYS = {
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "delete",
    "page-up",
    "page-down",
    "tab",
    "shift-tab",
    "control",
}

atexit.register(lambda: sys.stdout.write(SHOW_CURSOR))

OUTPUT_FORMAT = "{name}-{target}-v{version}{extension}"

Bundle = namedtuple("Bundle", ["command", "source", "compression"])
Target = namedtuple("Target", ["key", "label", "goos", "goarch", "extension", "archive"], defaults=[None, None, "", False])
Module = namedtuple("Module", ["key", "label", "binary_name", "path", "valid_targets", "bundle"], defaults=[None])

_BINARY_TARGETS = (
    Target("darwin-arm64",  "darwin-arm64",  "darwin",  "arm64"),
    Target("linux-amd64",   "linux-amd64",   "linux",   "amd64"),
    Target("linux-arm64",   "linux-arm64",   "linux",   "arm64"),
    Target("windows-amd64", "windows-amd64", "windows", "amd64", ".exe"),
    Target("windows-arm64", "windows-arm64", "windows", "arm64", ".exe"),
)
_BUNDLE_TARGET = Target("bundle", "bundle", archive=True)
_BINARY_KEYS   = frozenset(t.key for t in _BINARY_TARGETS)

TARGETS = _BINARY_TARGETS + (_BUNDLE_TARGET,)

MODULES = (
    Module("localboot",       "CLI",    "localboot",        ROOT / "applications" / "cli", _BINARY_KEYS),
    Module("localbootd",      "Daemon", "localbootd",       ROOT / "services" / "daemon",  _BINARY_KEYS),
    Module("localboot-webui", "WebUI",  "localboot-webui",  ROOT / "services" / "web-ui",  _BINARY_KEYS | {"bundle"},
           Bundle("bun run build", ".output", "gz")),
)


def main() -> int:
    args = parse_args()

    if args.module is None or args.target is None:
        print(f"{DIM}localboot build tool{RESET}")
        print()
    try:
        selected_modules = choose_items("Module", MODULES, args.module, header="┌", last=False)
        selected_targets = choose_items("Target", TARGETS, args.target, header="◆", last=True)
    except KeyboardInterrupt:
        print("\n◇  Canceled.")
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not selected_modules or not selected_targets:
        print("Nothing selected.")
        return 1

    missing = [m for m in selected_modules if not m.path.exists()]
    if missing:
        names = ", ".join(m.label for m in missing)
        print(f"error: module directories not found: {names}", file=sys.stderr)
        return 1

    ARTIFACTS.mkdir(exist_ok=True)

    print()
    results = build(selected_modules, selected_targets, verbose=args.verbose)
    elapsed_total = sum(elapsed for _, _, elapsed in results)
    failures = [(label, err) for label, err, _ in results if err]

    if failures:
        print()
        for label, err in failures:
            print(f"  ◆  {label} (failed)")
            for line in str(err).splitlines():
                print(f"     {DIM}{line}{RESET}")

    successful = len(results) - len(failures)
    print()
    print(f"◇  {successful} successful. {len(failures)} failed. {DIM}({elapsed_total} ms){RESET}")
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Localboot modules for selected targets.",
        usage="%(prog)s [-m MODULE] [-t TARGET]",
    )
    parser.add_argument(
        "-m",
        "--module",
        "--modules",
        dest="module",
        metavar="MODULE",
        help=f"Module to build: {', '.join(m.key for m in MODULES)}.",
    )
    parser.add_argument(
        "-t",
        "--target",
        "--targets",
        dest="target",
        metavar="TARGET",
        help=f"Build target: {', '.join(t.key for t in TARGETS)}.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print subprocess output.",
    )
    return parser.parse_args()


def choose_items(
    title: str,
    items: tuple[Module, ...] | tuple[Target, ...],
    raw_selection: str | None,
    *,
    header: str = "┌",
    last: bool = True,
) -> list[Module] | list[Target]:
    if raw_selection is not None:
        return select_from_raw(items, raw_selection)

    if not sys.stdin.isatty():
        raise ValueError(f"{title} selection requires a TTY or use -m/-t flags.")

    return select_with_tui(title, items, header=header, last=last)


def select_with_tui(
    title: str,
    items: tuple[Module, ...] | tuple[Target, ...],
    *,
    header: str = "┌",
    last: bool = True,
) -> list[Module] | list[Target]:
    cursor = 0
    selected: set[int] = set()
    search = ""
    line_count = 0
    first_render = True

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()
    try:
        while True:
            visible = filtered_indexes(items, search)
            if visible and cursor not in visible:
                cursor = visible[0]

            line_count = render_prompt(
                title, items, cursor, selected, search, visible, line_count, first_render,
                header=header, last=True,
            )
            first_render = False
            key = read_key()

            if key == "\x03":
                raise KeyboardInterrupt
            elif key in {"\x7f", "\b", "backspace"}:
                search = search[:-1]
            elif key in {"\x15", "\x1b"}:
                search = ""
            elif key in {"up", "k", "K"}:
                cursor = move_cursor(visible, cursor, -1)
            elif key in {"down", "j", "J"}:
                cursor = move_cursor(visible, cursor, 1)
            elif key in CONTROL_KEYS:
                pass
            elif key == " ":
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
            elif key in {"\r", "\n"}:
                if selected:
                    confirmed_header = "◇" if header == "◆" else header
                    all_visible = list(range(len(items)))
                    render_prompt(
                        title, items, -1, selected, "", all_visible, line_count, False,
                        tips=False, header=confirmed_header, last=last,
                    )
                    sys.stdout.write(SHOW_CURSOR)
                    sys.stdout.flush()
                    return [item for index, item in enumerate(items) if index in selected]
            elif is_search_key(key):
                search += key
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()


def render_prompt(
    title: str,
    items: tuple[Module, ...] | tuple[Target, ...],
    cursor: int,
    selected: set[int],
    search: str,
    visible: list[int],
    prev_line_count: int,
    first_render: bool,
    *,
    tips: bool = True,
    header: str = "┌",
    last: bool = True,
) -> int:
    if header == "┌":
        lines = [f"┌  Select {title.lower()}s:"]
    else:
        lines = [f"{header}  Select {title.lower()}s:"]
    lines.append("│")

    if search:
        n = len(visible)
        lines.append(f"│  {DIM}Search:{RESET} {search} {DIM}({n} {'match' if n == 1 else 'matches'}){RESET}")
        lines.append("│")

    if not visible:
        lines.append(f"│  {DIM}No matches.{RESET}")
    else:
        for index in visible:
            item = items[index]
            is_selected = index in selected
            is_hovered = index == cursor
            if is_selected:
                lines.append(f"│  ◼  {item.label}")
            elif is_hovered:
                lines.append(f"│  {DIM}◻{RESET}  {item.label}")
            else:
                lines.append(f"│  {DIM}◻  {item.label}{RESET}")
        if tips:
            lines.append("│")
            lines.append(f"│  {TIPS}")

    lines.append("└" if last else "│")

    if not first_render:
        sys.stdout.write(f"\033[{prev_line_count}A\033[J")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return len(lines)


def filtered_indexes(
    items: tuple[Module, ...] | tuple[Target, ...],
    search: str,
) -> list[int]:
    normalized_search = normalize(search)
    if not normalized_search:
        return list(range(len(items)))
    return [
        index
        for index, item in enumerate(items)
        if normalized_search in normalize(item.key)
        or normalized_search in normalize(item.label)
    ]


def move_cursor(visible_indexes: list[int], cursor: int, step: int) -> int:
    if not visible_indexes:
        return cursor
    if cursor not in visible_indexes:
        return visible_indexes[0]
    visible_position = visible_indexes.index(cursor)
    return visible_indexes[(visible_position + step) % len(visible_indexes)]


def is_search_key(key: str) -> bool:
    return len(key) == 1 and key.isprintable()


def select_from_raw(
    items: tuple[Module, ...] | tuple[Target, ...],
    raw_selection: str,
) -> list[Module] | list[Target]:
    tokens = [token.strip().lower() for token in raw_selection.split(",") if token.strip()]
    if not tokens:
        raise ValueError("Please select at least one item.")

    selected = []
    for token in tokens:
        item = find_item(items, token)
        if item in selected:
            continue
        selected.append(item)
    return selected


def find_item(
    items: tuple[Module, ...] | tuple[Target, ...],
    token: str,
) -> Module | Target:
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(items):
            return items[index - 1]

    normalized = normalize(token)
    for item in items:
        aliases = {item.key, normalize(item.key), normalize(item.label)}
        if normalized in aliases:
            return item

    names = ", ".join(item.key for item in items)
    raise ValueError(f"Unknown selection '{token}'. Valid values: {names}.")


def normalize(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "-")
        .replace("_", "-")
        .replace(".", "")
        .replace("(", "")
        .replace(")", "")
    )


def read_key() -> str:
    if msvcrt is not None:
        char = msvcrt.getwch()
        if char in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            if code == "H":
                return "up"
            if code == "P":
                return "down"
            if code == "K":
                return "left"
            if code == "M":
                return "right"
            if code == "G":
                return "home"
            if code == "O":
                return "end"
            if code == "S":
                return "delete"
            if code == "I":
                return "page-up"
            if code == "Q":
                return "page-down"
            return "control"
        if char == "\t":
            return "tab"
        if char == "\b":
            return "backspace"
        return "\n" if char == "\r" else char

    if termios is None or tty is None:
        return sys.stdin.read(1)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = os.read(fd, 1).decode(errors="ignore")
        if char == "\x1b":
            return decode_escape_sequence(read_escape_sequence(fd))
        if char == "\t":
            return "tab"
        return char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def read_escape_sequence(fd: int) -> str:
    sequence = "\x1b"
    while len(sequence) < 16 and select.select([fd], [], [], 0.2)[0]:
        sequence += os.read(fd, 1).decode(errors="ignore")
        if sequence[-1].isalpha() or sequence[-1] == "~":
            break
    return sequence


def decode_escape_sequence(sequence: str) -> str:
    if sequence == "\x1b":
        return "\x1b"
    if sequence.startswith("\x1b[") and sequence[-1:] in {"A", "B", "C", "D", "H", "F"}:
        return {
            "A": "up",
            "B": "down",
            "C": "right",
            "D": "left",
            "H": "home",
            "F": "end",
        }[sequence[-1]]
    if sequence.startswith("\x1bO") and sequence[-1:] in {"A", "B", "C", "D", "H", "F"}:
        return {
            "A": "up",
            "B": "down",
            "C": "right",
            "D": "left",
            "H": "home",
            "F": "end",
        }[sequence[-1]]
    if sequence in {"\x1b[A", "\x1bOA"}:
        return "up"
    if sequence in {"\x1b[B", "\x1bOB"}:
        return "down"
    if sequence in {"\x1b[C", "\x1bOC"}:
        return "right"
    if sequence in {"\x1b[D", "\x1bOD"}:
        return "left"
    if sequence in {"\x1b[H", "\x1bOH"}:
        return "home"
    if sequence in {"\x1b[F", "\x1bOF"}:
        return "end"
    if sequence == "\x1b[3~":
        return "delete"
    if sequence == "\x1b[5~":
        return "page-up"
    if sequence == "\x1b[6~":
        return "page-down"
    if sequence == "\x1b[Z":
        return "shift-tab"
    return "control"


def build(
    modules: list[Module], targets: list[Target], *, verbose: bool = False
) -> list[tuple[str, BuildError | None, int]]:
    results: list[tuple[str, BuildError | None, int]] = []

    for module in modules:
        module_path = module.path
        applicable = [t for t in targets if t.key in module.valid_targets]

        if module.bundle is not None and any(t.archive for t in applicable):
            print(f"  {DIM}bundling assets...{RESET}", end="", flush=True)
            try:
                build_assets(module_path, module.bundle, verbose=verbose)
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
            except BuildError as err:
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
                results.append((f"{module.label} assets", err, 0))
                continue

        for target in applicable:
            if target.archive:
                results.append(build_archive(module, module_path))
            else:
                results.append(build_binary(module, module_path, target, verbose=verbose))

    return results


def build_assets(module_path: Path, bundle: Bundle, *, verbose: bool = False) -> None:
    source = module_path / bundle.source
    if source.exists():
        shutil.rmtree(source)
    run(tuple(bundle.command.split()), cwd=module_path, verbose=verbose)


def build_binary(
    module: Module, module_path: Path, target: Target, *, verbose: bool = False
) -> tuple[str, BuildError | None, int]:
    label = f"{module.label} for {target.label}"

    version_file = module_path / "version.txt"
    version = version_file.read_text().strip() if version_file.exists() else "0.0.0"
    filename = OUTPUT_FORMAT.format(
        name=module.binary_name, target=target.key, version=version, extension=target.extension,
    )
    output_dir = ARTIFACTS / module.key
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    env = os.environ.copy()
    env.update({"CGO_ENABLED": "0", "GOOS": target.goos, "GOARCH": target.goarch})

    print(f"  {DIM}building {label}...{RESET}", end="", flush=True)
    start = time.monotonic()
    try:
        run(("go", "build", "-o", str(output_path), "."), cwd=module_path, env=env, verbose=verbose)
    except BuildError as err:
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()
        return label, err, 0
    elapsed = int((time.monotonic() - start) * 1000)
    print(f"\r\033[2K  ◇  {label} {DIM}({elapsed} ms){RESET}")
    print(f"     {DIM}→ {output_path.relative_to(ROOT)}{RESET}")
    return label, None, elapsed


def build_archive(
    module: Module, module_path: Path
) -> tuple[str, BuildError | None, int]:
    bundle = module.bundle
    label = f"{module.label} bundle"
    source = module_path / bundle.source

    if not source.exists():
        return label, BuildError(f"{bundle.source} directory was not generated"), 0

    tar_mode = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz", "zst": "w:zst"}.get(bundle.compression, "w:gz")
    archive_ext = f".tar.{bundle.compression}"

    version_file = module_path / "version.txt"
    version = version_file.read_text().strip() if version_file.exists() else "0.0.0"
    filename = OUTPUT_FORMAT.format(
        name=module.binary_name, target="bundle", version=version, extension=archive_ext,
    )
    archive_dir = ARTIFACTS / module.key
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / filename

    print(f"  {DIM}archiving bundle...{RESET}", end="", flush=True)
    start = time.monotonic()
    try:
        with tarfile.open(archive_path, tar_mode, compresslevel=9) as archive:
            for path in sorted(source.rglob("*")):
                archive.add(path, arcname=path.relative_to(source))
    except Exception as err:
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()
        return label, BuildError(str(err)), 0
    elapsed = int((time.monotonic() - start) * 1000)
    print(f"\r\033[2K  ◇  {label} {DIM}({elapsed} ms){RESET}")
    print(f"     {DIM}→ {archive_path.relative_to(ROOT)}{RESET}")
    return label, None, elapsed


def run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    verbose: bool = False,
) -> None:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, check=True, capture_output=True, text=True
        )
        if verbose:
            output = (result.stdout + result.stderr).strip()
            if output:
                print("\n")
                for line in output.splitlines():
                    print(f"     {DIM}{line}{RESET}")
                print()
    except FileNotFoundError as error:
        raise BuildError(f"missing command: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        output = (error.stderr or error.stdout or "").strip()
        raise BuildError(output or f"command failed: {' '.join(command)}") from error


class BuildError(Exception):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
