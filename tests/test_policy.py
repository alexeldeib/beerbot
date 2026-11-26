"""Invariants every automated change must keep. Part of beerdev's kernel: the ship job refuses any
change to this file, so beerdev can change workflows, scripts, and infrastructure only within these
bounds. The owner changes them by hand. These checks catch mistakes; the independent reviewer in
beerdev-build.yml catches intent."""

import asyncio
import os
import re
from pathlib import Path

import yaml

# The review job runs this file from outside the patched tree, pointing here at it.
ROOT = Path(os.environ.get("POLICY_ROOT") or Path(__file__).resolve().parents[1])
WORKFLOWS = {
    path.name: yaml.safe_load(path.read_text())
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml"))
}

# Where each secret may be read. A new use anywhere is the owner's call.
SECRETS = {
    ("beerdev-build.yml", "implement"): {"CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY"},
    ("beerdev-build.yml", "review"): {"CLAUDE_CODE_OAUTH_TOKEN"},
    ("beerdev-build.yml", "ship"): {"BEERDEV_BOT_ID", "BEERDEV_SHIP_TOKEN"},
    ("beerdev-lead.yml", "plan"): {"CLAUDE_CODE_OAUTH_TOKEN"},
    ("beerdev.yml", "digest"): {"BEERDEV_BOT_ID"},
    ("ci.yml", "deploy"): {"FLY_API_TOKEN"},
    ("ops.yml", "check"): {"OPS_TOKEN", "OPS_ALERT_BOT_ID"},  # Never the admin token.
    ("rollback.yml", "rollback"): {"FLY_API_TOKEN"},
}
# Jobs whose GITHUB_TOKEN may write, and to what.
WRITES = {
    ("beerdev-build.yml", "ship"): {"actions", "contents", "issues", "pull-requests"},
    ("beerdev-build.yml", "flag"): {"issues"},
    ("beerdev-lead.yml", "file"): {"actions", "issues"},
    ("ops.yml", "check"): {"actions", "issues"},
}
# None of these runs code from forks or from issue and comment text.
TRIGGERS = {"push", "pull_request", "schedule", "workflow_dispatch", "workflow_run"}
# The only guard that keeps a job holding credentials out of pull request runs.
NOT_A_PR = "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
# The only hosts code holding secrets may name.
HOSTS = {"github.com", "api.github.com", "api.groupme.com", "beerbot-groupme.fly.dev"}
KERNEL = [
    ".github/workflows/beerdev-build.yml",
    ".github/workflows/beerdev-lead.yml",
    ".github/beerdev-prompt.md",
    ".github/beerdev-review-prompt.md",
    ".github/beerdev-lead-prompt.md",
    ".claude/settings.json",
    "tests/test_policy.py",
]


def jobs():
    for name, workflow in WORKFLOWS.items():
        for job, spec in workflow["jobs"].items():
            yield name, job, spec, yaml.safe_dump(spec)


def hosts(text: str) -> set[str]:
    return set(re.findall(r"https?://(?:[^@/\s\"']+@)?([A-Za-z0-9.-]+)", text))


def test_permissions_are_explicit_and_least_privilege():
    for name, workflow in WORKFLOWS.items():
        assert workflow.get("permissions") in ({}, {"contents": "read"}), name
    for name, job, spec, _ in jobs():
        permissions = spec.get("permissions") or {}
        assert isinstance(permissions, dict), f"{name}/{job}: {permissions}"
        writes = {scope for scope, level in permissions.items() if level == "write"}
        assert writes <= WRITES.get((name, job), set()), f"{name}/{job} writes {writes}"


def test_triggers_never_hand_secrets_to_untrusted_code():
    for name, workflow in WORKFLOWS.items():
        on = set(triggers(workflow))
        assert on <= TRIGGERS, f"{name}: {on - TRIGGERS}"
        if "workflow_run" in on:  # Fork branches can carry this repository's names.
            for job, spec in workflow["jobs"].items():
                guard = "github.event.workflow_run.head_repository.full_name == github.repository"
                assert guard in str(spec.get("if")), f"{name}/{job}"


def triggers(workflow: dict) -> dict:
    on = workflow.get(True, workflow.get("on"))  # YAML 1.1 reads a bare `on` as True.
    if isinstance(on, (str, list)):  # `on: push` and `on: [push]` cover every branch.
        return {name: None for name in ([on] if isinstance(on, str) else on)}
    return on


def holds_credentials(spec: dict, text: str) -> bool:
    writes = "write" in (spec.get("permissions") or {}).values()
    return "secrets." in text or "environment" in spec or writes


def test_branch_pushes_and_pull_requests_never_reach_credentials():
    # beerdev pushes branches and opens PRs with a token that starts workflows, which then run
    # from the branch's own (agent-written) files: only main may run code that holds credentials.
    for name, workflow in WORKFLOWS.items():
        on = triggers(workflow)
        if "push" in on:
            assert on["push"] == {"branches": ["main"]}, f"{name}: push {on['push']}"
        if "pull_request" in on:
            for job, spec in workflow["jobs"].items():
                if holds_credentials(spec, yaml.safe_dump(spec)):
                    assert spec.get("if") == NOT_A_PR, f"{name}/{job} runs in pull requests"


def test_jobs_holding_credentials_never_restore_caches():
    # Jobs that run agent-written code can save caches; a poisoned one would run here.
    for name, job, spec, text in jobs():
        if holds_credentials(spec, text):
            for step in spec.get("steps", []):
                uses = str(step.get("uses", ""))
                assert not uses.startswith("actions/cache"), f"{name}/{job}"
                if "setup-uv" in uses:  # Its default caches on GitHub-hosted runners.
                    assert step.get("with", {}).get("enable-cache") is False, f"{name}/{job}"


def test_secrets_stay_in_their_jobs():
    for name, workflow in WORKFLOWS.items():
        outside_jobs = {key: value for key, value in workflow.items() if key != "jobs"}
        assert "secrets" not in yaml.safe_dump(outside_jobs), f"{name}: workflow-wide secrets"
    for name, job, spec, text in jobs():
        # Reusable workflows can inherit every secret; toJSON(secrets) dumps them all.
        assert "uses" not in spec and "secrets" not in spec, f"{name}/{job}: reusable workflow"
        expressions = " ".join(re.findall(r"\$\{\{(.*?)\}\}", text, re.S))
        assert not re.search(r"\bsecrets\b(?!\.\w)", expressions), f"{name}/{job}: bulk secrets"
        used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", expressions))
        assert used <= SECRETS.get((name, job), set()), f"{name}/{job} reads {used}"


def test_code_holding_secrets_only_names_known_hosts():
    for name, job, spec, text in jobs():
        if "secrets." in text:
            scripts = "\n".join(step.get("run", "") for step in spec.get("steps", []))
            assert hosts(scripts) <= HOSTS, f"{name}/{job} reaches {hosts(scripts) - HOSTS}"
    for script in (ROOT / "scripts").glob("*.py"):
        found = hosts(script.read_text())
        assert found <= HOSTS, f"{script.name} reaches {found - HOSTS}"


def test_agents_never_hold_write_access():
    for name, job, spec, text in jobs():
        if "claude-code" in text:
            levels = set((spec.get("permissions") or {}).values())
            assert levels <= {"read"}, f"{name}/{job} runs an agent with {levels}"
            for step in spec["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    assert step.get("with", {}).get("persist-credentials") is False, f"{name}/{job}"
            assert "Read(//proc/**)" in text, f"{name}/{job}: the agent can read its own env"


def test_deploys_verify_production_and_roll_back():
    deploy = WORKFLOWS["ci.yml"]["jobs"]["deploy"]
    assert "test" in deploy["needs"]
    assert "refs/heads/main" in deploy["if"] and "pull_request" in deploy["if"]
    steps = deploy["steps"]
    ids = {step["id"]: step for step in steps if "id" in step}
    assert {"previous", "verify"} <= set(ids)
    for check in ("/health", "/ready", "/version", "/ready/db"):
        assert check in ids["verify"]["run"]
    assert any("--strategy bluegreen" in str(step.get("run")) for step in steps)
    rollback = [s for s in steps if "steps.verify.outcome == 'failure'" in str(s.get("if"))]
    assert rollback and "--image" in rollback[0]["run"] and "/ready/db" in rollback[0]["run"]


def test_beerdev_cannot_change_its_kernel():
    ship = WORKFLOWS["beerdev-build.yml"]["jobs"]["ship"]
    text = "\n".join(step.get("run", "") for step in ship["steps"])
    kernel = re.search(r"kernel='([^']+)'", text).group(1)
    for path in KERNEL:
        assert re.search(kernel, path), path
    assert not re.search(kernel, "src/beerbot/tools.py")
    assert "--no-renames" in text  # A rename must not hide a kernel file.
    assert "(pyproject\\.toml|uv\\.lock)" in text  # Dependency changes always wait for the owner.
    review = WORKFLOWS["beerdev-build.yml"]["jobs"]["review"]
    assert "review" in ship["needs"] and "claude-code" in yaml.safe_dump(review)


# The review job skips the two app checks below (`-k "not app"`): they import agent-written code.
def test_app_lets_only_the_owner_steer_beerdev():
    from src.beerbot import beerdev

    ctx = beerdev.Context("g1", "m1", False, "beerdev veto #31", "")
    steer = next(tool for tool in beerdev.create_tools(ctx) if tool.__name__ == "steer")
    try:
        result = asyncio.run(steer("veto", issue=31))
    except ValueError:
        result = {"error": "refused"}
    assert "error" in result


def test_app_webhooks_and_admin_endpoints_require_their_secrets(monkeypatch):
    from fastapi.testclient import TestClient

    from src.beerbot import main

    # Every admin route needs the admin token; ops' token reaches exactly these.
    ops_routes = {
        ("GET", "/admin/workers/status"),
        ("GET", "/admin/messages/status"),
        ("GET", "/admin/usage"),
        ("POST", "/admin/messages/wake"),  # A harmless nudge, never a restart.
    }
    admin = [route for route in main.app.routes if getattr(route, "path", "").startswith("/admin")]
    for route in admin:
        calls = [dependency.call for dependency in route.dependant.dependencies]
        if {(method, route.path) for method in route.methods} <= ops_routes:
            assert main.verify_ops_token in calls, route.path
        else:
            assert main.verify_admin_token in calls, route.path
    for workflow in (ROOT / ".github/workflows").glob("*.y*ml"):
        assert "ADMIN_TOKEN" not in workflow.read_text(), workflow.name
    monkeypatch.setattr(main.settings, "groupme_webhook_secret", "expected")
    monkeypatch.setattr(main.settings, "admin_token", "expected")
    monkeypatch.setattr(main.settings, "ops_token", "reader")
    with TestClient(main.app) as client:
        assert client.post("/callback?token=forged", json={}).status_code == 401
        assert client.get("/admin/groups").status_code == 401
        assert (
            client.get("/admin/groups", headers={"Authorization": "Bearer reader"}).status_code
            == 403
        )


def test_the_rollback_restores_a_known_release_and_verifies():
    workflow = WORKFLOWS["rollback.yml"]
    on = triggers(workflow)
    assert set(on) == {"workflow_dispatch"}  # Started by ops or by hand only.
    assert set(on["workflow_dispatch"]["inputs"]) == {"dry_run", "version"}
    job = workflow["jobs"]["rollback"]
    assert job["environment"] == "production"  # main-only: a branch can't roll production back.
    assert (
        workflow["concurrency"]["group"]
        == WORKFLOWS["ci.yml"]["jobs"]["deploy"]["concurrency"]["group"]
    )
    steps = {step.get("id") or step.get("name"): step for step in job["steps"]}
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "inputs." not in scripts  # Inputs reach scripts as env, never as code.
    target = steps["target"]["run"]
    # After any rollback, "the release before" is the broken one: refuse unless the owner names one.
    assert "$VERSION" in target and "rolled back" in target
    # The target's own fly.toml, found through the commit CI labels each image with.
    assert ':fly.toml" > fly.toml' in target
    ci_deploy = "\n".join(
        str(step.get("run")) for step in WORKFLOWS["ci.yml"]["jobs"]["deploy"]["steps"]
    )
    assert '--image-label "${GITHUB_SHA}"' in ci_deploy
    assert "--strategy bluegreen" in scripts
    for name in ("Deploy it blue-green", "Verify production"):
        assert steps[name]["if"] == "${{ !inputs.dry_run }}", name
    verify = steps["Verify production"]["run"]
    assert all(check in verify for check in ("health", "ready", "ready/db"))
