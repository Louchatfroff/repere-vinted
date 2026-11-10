"""Create a private GitHub repository from the reviewed local Git commit.

Requires GitHub CLI authenticated on the owner's machine. Never uploads data/.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)


def main():
    parser = argparse.ArgumentParser(description="Publish this project to a new private GitHub repository")
    parser.add_argument("repository", help="OWNER/NAME, for example antoinebrnl/repere-vinted")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected OWNER/NAME")
    try:
        # Query the identity used for actual API calls. `auth status` also fails
        # when an unrelated, inactive saved account has an expired token.
        login = run("gh", "api", "--hostname", "github.com", "user", "--jq", ".login", capture=True).stdout.strip()
        if not login:
            raise RuntimeError("GitHub did not return an authenticated account.")
        print("Active GitHub account:", login)
        if run("git", "status", "--porcelain", capture=True).stdout.strip():
            raise RuntimeError("Commit the reviewed source changes before publishing.")
        files = run("git", "ls-files", capture=True).stdout.splitlines()
        for file in files:
            path = Path(file)
            if any(part in {"data", ".venv", "node_modules", "test-artifacts"} for part in path.parts) or path.name.startswith(".env") or ".sqlite" in path.name or path.suffix in {".log", ".pem", ".key"}:
                raise RuntimeError("Private/generated file is tracked: " + file)
        run("gh", "repo", "create", args.repository, "--private", "--description", "Repère — personal local Vinted monitoring", "--source", ".", "--remote", "origin", "--push")
        metadata = json.loads(run("gh", "repo", "view", args.repository, "--json", "visibility,url", capture=True).stdout)
        if metadata["visibility"] != "PRIVATE": raise RuntimeError("Repository visibility needs immediate verification.")
        print("Private repository:", metadata["url"])
        print("Invite collaborators through Settings > Collaborators. No invitations were sent by this script.")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print("Publication stopped:", error)
        raise SystemExit(1)


if __name__ == "__main__": main()
