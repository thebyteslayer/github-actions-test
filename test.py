import subprocess


def run(cmd):
    """Run a shell command and stream output."""
    print(f"\nRunning: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")


def main():
    version = input("Enter version number: ").strip()

    if not version:
        print("Version cannot be empty")
        return

    tag = f"v{version}"

    commit_message = f"{version}"
    release_message = f"Release {tag}"

    # Git commands
    run(["git", "add", "."])
    run(["git", "commit", "-m", commit_message])
    run(["git", "push"])
    run(["git", "tag", "-a", tag, "-m", release_message])
    run(["git", "push", "origin", tag])

    print(f"\nRelease {tag} completed successfully!")


if __name__ == "__main__":
    main()