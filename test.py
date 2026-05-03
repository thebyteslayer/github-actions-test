import subprocess


def run(cmd, check=True):
    """Run a shell command and stream output."""
    print(f"\nRunning: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")
    return result.returncode


def main():
    version = input("Enter version number: ").strip()

    if not version:
        print("Version cannot be empty")
        return

    tag = f"v{version}"

    commit_message = f"{version}"
    release_message = f"Release {tag}"

    # Stage changes
    run(["git", "add", "."])

    # Try to commit (but don't fail if nothing to commit)
    commit_rc = run(["git", "commit", "-m", commit_message], check=False)
    if commit_rc != 0:
        print("No changes to commit, continuing...")
    else:
        run(["git", "push"])

    # Create tag (don't fail if it already exists)
    tag_rc = run(["git", "tag", "-a", tag, "-m", release_message], check=False)
    if tag_rc != 0:
        print(f"Tag {tag} may already exist, continuing...")

    # Push tag
    run(["git", "push", "origin", tag])

    print(f"\nRelease {tag} completed successfully!")


if __name__ == "__main__":
    main()