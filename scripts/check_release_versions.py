"""Report dependencies that have newer releases before publishing an image."""

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WORKFLOWS = Path(".github/workflows")
ACTION_REF = re.compile(r"^\s*-?\s*uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s*(?:#.*)?$", re.MULTILINE)
TRIVY_VERSION = re.compile(r"^\s*version:\s*(v\d+\.\d+\.\d+)\s*$", re.MULTILINE)


def github_json(path: str, token: str) -> dict[str, object]:
    request = Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=20) as response:
        import json

        return json.load(response)


def latest_action_commit(repository: str, token: str) -> tuple[str, str]:
    release = github_json(f"repos/{repository}/releases/latest", token)
    tag = str(release["tag_name"])
    ref = github_json(f"repos/{repository}/git/ref/tags/{tag}", token)
    target = ref["object"]
    if not isinstance(target, dict):
        raise ValueError(f"Unexpected tag response for {repository}")
    while target["type"] == "tag":
        tag_object = github_json(f"repos/{repository}/git/tags/{target['sha']}", token)
        target = tag_object["object"]
        if not isinstance(target, dict):
            raise ValueError(f"Unexpected annotated tag for {repository}")
    if target["type"] != "commit":
        raise ValueError(f"Latest release is not a commit for {repository}")
    return tag, str(target["sha"])


def workflow_versions(token: str) -> list[str]:
    findings: list[str] = []
    actions: dict[str, set[str]] = {}
    trivy_versions: set[str] = set()
    for workflow in WORKFLOWS.glob("*.yml"):
        source = workflow.read_text()
        for repository, commit in ACTION_REF.findall(source):
            actions.setdefault(repository, set()).add(commit)
        if "aquasecurity/trivy-action@" in source:
            trivy_versions.update(TRIVY_VERSION.findall(source))

    for repository, commits in sorted(actions.items()):
        latest_tag, latest_commit = latest_action_commit(repository, token)
        for commit in commits:
            if commit != latest_commit:
                findings.append(
                    f"GitHub Action `{repository}`: `{commit[:12]}`; latest `{latest_tag}` (`{latest_commit[:12]}`)"
                )

    release = github_json("repos/aquasecurity/trivy/releases/latest", token)
    latest_trivy = str(release["tag_name"])
    for version in trivy_versions:
        if version != latest_trivy:
            findings.append(f"Trivy scanner: `{version}`; latest `{latest_trivy}`")
    if not trivy_versions:
        raise ValueError("Trivy scanner version is not pinned in a workflow")
    return findings


def python_versions() -> list[str]:
    result = subprocess.run(
        ["uv", "tree", "--outdated", "--locked", "--all-groups"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [f"Python: `{line.lstrip(' │├└─')}`" for line in result.stdout.splitlines() if "(latest:" in line]


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required to check GitHub Action releases", file=sys.stderr)
        return 2

    try:
        findings = python_versions() + workflow_versions(token)
    except (HTTPError, URLError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Version check failed: {exc}", file=sys.stderr)
        return 2

    lines = ["### Release dependency freshness", ""]
    lines.extend(f"- {finding}" for finding in findings)
    if not findings:
        lines.append("All checked dependencies use their latest published releases.")
    report = "\n".join(lines) + "\n"
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as handle:
            handle.write(report)
    if findings:
        print(f"::warning title=Outdated release dependencies::{len(findings)} newer versions are available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
