#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections import namedtuple
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"

RESET = "\033[0m"
DIM = "\033[2m"

OUTPUT_FORMAT = "{name}-{target}-v{version}{extension}"

Bundle = namedtuple("Bundle", ["command", "source", "compression"])
Target = namedtuple("Target", ["key", "label", "goos", "goarch", "extension", "archive"], defaults=[None, None, "", False])
Module = namedtuple("Module", ["key", "label", "binary_name", "path", "valid_targets", "bundle", "artifact_dir"], defaults=[None, None])

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
    Module("localboot",       "CLI",    "localboot",        ROOT / "applications" / "cli", _BINARY_KEYS,              None,                              "cli"),
    Module("localbootd",      "Daemon", "localbootd",       ROOT / "services" / "daemon",  _BINARY_KEYS,              None,                              "daemon"),
    Module("localboot-webui", "WebUI",  "localboot-webui",  ROOT / "services" / "web-ui",  _BINARY_KEYS | {"bundle"}, Bundle("bun run build", ".output", "gz"), "web-ui"),
)


class BuildError(Exception):
    pass


def main() -> int:
    args = parse_args()

    try:
        selected_modules = select_from_raw(MODULES, args.module) if args.module else list(MODULES)
        selected_targets = select_from_raw(TARGETS, args.target) if args.target else list(TARGETS)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    missing = [m for m in selected_modules if not m.path.exists()]
    if missing:
        names = ", ".join(m.label for m in missing)
        print(f"error: module directories not found: {names}", file=sys.stderr)
        return 1

    ARTIFACTS.mkdir(exist_ok=True)

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
        description="CI build script for Localboot modules.",
        usage="%(prog)s [-m MODULE] [-t TARGET]",
    )
    parser.add_argument(
        "-m",
        "--module",
        "--modules",
        dest="module",
        metavar="MODULE",
        help=f"Modules to build (comma-separated): {', '.join(m.key for m in MODULES)}. Defaults to all.",
    )
    parser.add_argument(
        "-t",
        "--target",
        "--targets",
        dest="target",
        metavar="TARGET",
        help=f"Targets to build (comma-separated): {', '.join(t.key for t in TARGETS)}. Defaults to all.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print subprocess output.",
    )
    return parser.parse_args()


def select_from_raw(
    items: tuple,
    raw_selection: str,
) -> list:
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


def find_item(items: tuple, token: str):
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


def build(
    modules: list, targets: list, *, verbose: bool = False
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
    module, module_path: Path, target, *, verbose: bool = False
) -> tuple[str, BuildError | None, int]:
    label = f"{module.label} for {target.label}"

    version_file = module_path / "version.txt"
    version = version_file.read_text().strip() if version_file.exists() else "0.0.0"
    filename = OUTPUT_FORMAT.format(
        name=module.binary_name, target=target.key, version=version, extension=target.extension,
    )
    output_dir = ARTIFACTS / (module.artifact_dir or module.key)
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


def build_archive(module, module_path: Path) -> tuple[str, BuildError | None, int]:
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
    archive_dir = ARTIFACTS / (module.artifact_dir or module.key)
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


if __name__ == "__main__":
    raise SystemExit(main())
