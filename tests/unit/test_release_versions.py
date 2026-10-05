from pathlib import Path

from scripts import check_release_versions


def test_annotated_action_tag_resolves_to_commit(monkeypatch) -> None:
    responses = {
        "repos/example/action/releases/latest": {"tag_name": "v2.0.0"},
        "repos/example/action/git/ref/tags/v2.0.0": {"object": {"type": "tag", "sha": "tag-sha"}},
        "repos/example/action/git/tags/tag-sha": {"object": {"type": "commit", "sha": "commit-sha"}},
    }
    monkeypatch.setattr(check_release_versions, "github_json", lambda path, token: responses[path])

    assert check_release_versions.latest_action_commit("example/action", "token") == ("v2.0.0", "commit-sha")


def test_workflow_report_detects_stale_action_and_scanner(monkeypatch, tmp_path: Path) -> None:
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "security.yml").write_text(
        "uses: example/action@" + "a" * 40 + " # v1\n"
        "uses: aquasecurity/trivy-action@" + "b" * 40 + " # v0.36.0\n"
        "version: v0.70.0\n"
    )
    monkeypatch.setattr(check_release_versions, "WORKFLOWS", workflow_dir)
    monkeypatch.setattr(
        check_release_versions,
        "latest_action_commit",
        lambda repository, token: ("v2.0.0", "c" * 40) if repository == "example/action" else ("v0.36.0", "b" * 40),
    )
    monkeypatch.setattr(
        check_release_versions,
        "github_json",
        lambda path, token: {"tag_name": "v0.75.0"},
    )

    findings = check_release_versions.workflow_versions("token")

    assert len(findings) == 2
    assert "example/action" in findings[0]
    assert "Trivy scanner" in findings[1]
