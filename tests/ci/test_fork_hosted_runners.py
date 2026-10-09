"""UPX-1974: fork CI stays schedulable without changing upstream coverage."""
from pathlib import Path

import hermes_yaml

from tests.ci.desktop_release_roles import evaluate

ROOT = Path(__file__).resolve().parents[2]


def test_fork_ci_preserves_native_hosts_and_test_selection():
    upstream = "NousResearch/hermes-agent"
    fork = "leonardsellem/hermes-agent"
    workflows = {
        "tests.yml": {"test": ("ubuntu-latest-32-core", "ubuntu-latest", 32, 4),
                      "e2e": ("ubuntu-latest-32-core", "ubuntu-latest", 3, 1),
                      "e2e-upgrade": ("ubuntu-latest-32-core", "ubuntu-latest", 6, 1)},
        "tests-os.yml": {"e2e-windows": ("windows-latest-32-core", "windows-latest", 6, 1)},
        "windows-install-update-e2e.yml": {"install-update": ("windows-latest-32-core", "windows-latest", 6, 1)},
        "nix.yml": {"flake-check": ("ubuntu-latest-32-core", "ubuntu-latest", None, None)},
        "js-tests.yml": {"check": ("ubuntu-latest-32-core", "ubuntu-latest", None, None)},
        "rust-tests.yml": {"bootstrap-installer": ("ubuntu-latest-32-core", "ubuntu-latest", None, None)},
        "e2e-desktop-core.yml": {"core": ("ubuntu-latest-32-core", "ubuntu-latest", None, None)},
        "e2e-desktop-update.yml": {"update": ("ubuntu-latest-32-core", "ubuntu-latest", None, None)},
    }
    docs = {name: hermes_yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            for name in workflows}
    for name, jobs in workflows.items():
        for job_id, (large, standard, upstream_workers, fork_workers) in jobs.items():
            job = docs[name]["jobs"][job_id]
            for repo, runner, workers in ((upstream, large, upstream_workers), (fork, standard, fork_workers)):
                context = {"repository": repo}
                selected = evaluate(job["runs-on"], {}, {}, github=context) if job["runs-on"].startswith("${{") else job["runs-on"]
                assert selected == runner, (name, job_id, repo)
                if workers is not None:
                    (step,) = [s for s in job["steps"] if "HERMES_TEST_WORKERS" in s.get("env", {})]
                    assert str(evaluate(step["env"]["HERMES_TEST_WORKERS"], {}, {}, github=context)) == str(workers)
    (release_refs,) = [s for s in docs["tests.yml"]["jobs"]["e2e-upgrade"]["steps"]
                       if s.get("name") == "Fetch official release refs for fork upgrade fixtures"]
    assert evaluate(release_refs["if"], {}, {}, github={"repository": fork}) is True
    assert evaluate(release_refs["if"], {}, {}, github={"repository": upstream}) is False
    assert release_refs["run"].strip() == (
        "git fetch --no-tags https://github.com/NousResearch/hermes-agent.git "
        "'refs/tags/v20*:refs/tags/v20*'")
    assert docs["tests.yml"]["jobs"]["e2e-upgrade"]["steps"].index(release_refs) == 1
    (js_step,) = [s for s in docs["js-tests.yml"]["jobs"]["check"]["steps"]
                  if s.get("name") == "Run all workspace checks"]
    command, expression = js_step["run"].split("${{", 1)
    assert command.strip() == "node .github/scripts/run-workspace-checks.mjs"
    assert evaluate("${{" + expression, {}, {}, github={"repository": fork}) == "--concurrency 1"
    assert evaluate("${{" + expression, {}, {}, github={"repository": upstream}) == ""
    for name, job_id, fork_minutes, upstream_minutes in (
            ("tests.yml", "test", 150, 30),
            ("tests.yml", "e2e", 120, 30),
            ("windows-install-update-e2e.yml", "install-update", 100, 50)):
        timeout = docs[name]["jobs"][job_id]["timeout-minutes"]
        assert evaluate(timeout, {}, {}, github={"repository": fork}) == fork_minutes, (name, job_id)
        assert evaluate(timeout, {}, {}, github={"repository": upstream}) == upstream_minutes, (name, job_id)
    os_job = docs["tests-os.yml"]["jobs"]["os-tests"]
    rows = os_job["strategy"]["matrix"]["include"]
    assert [(r["marker"], r["runner"]) for r in rows] == [
        ("macos", "macos-latest"), ("windows", "windows-latest-32-core"),
        ("windows", "windows-latest-32-arm-core")]
    for repo, expected in ((upstream, ["macos-latest", "windows-latest-32-core", "windows-latest-32-arm-core"]),
                           (fork, ["macos-latest", "windows-latest", "windows-11-arm"])):
        actual = [evaluate(os_job["runs-on"].replace("matrix.runner", repr(r["runner"])), {}, {},
                           github={"repository": repo}) for r in rows]
        assert actual == expected
    (os_step,) = [s for s in os_job["steps"] if "HERMES_TEST_WORKERS" in s.get("env", {})]
    for repo, workers in ((upstream, "16"), (fork, "2")):
        for arch in ("X64", "ARM64"):
            expr = os_step["env"]["HERMES_TEST_WORKERS"].replace("matrix.marker", "'windows'").replace("runner.arch", repr(arch))
            assert evaluate(expr, {}, {}, github={"repository": repo}) == workers
    assert 'list_os_marked_tests.py' in os_step["run"]
    assert '-m "platforms and not integration"' in os_step["run"]
    assert 'if [ ! -s "$LIST" ]' in os_step["run"]
    assert 'scripts/run_tests.sh\n' in docs["tests.yml"]["jobs"]["test"]["steps"][-2]["run"]
    for name, job_id in (("tests.yml", "e2e"), ("tests-os.yml", "e2e-windows"),
                         ("windows-install-update-e2e.yml", "install-update")):
        (step,) = [s for s in docs[name]["jobs"][job_id]["steps"] if "HERMES_TEST_FILE_RETRIES" in s.get("env", {})]
        assert step["env"]["HERMES_TEST_FILE_RETRIES"] == "0"
        assert "scripts/run_tests.sh" in step["run"]
        for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "NOUS_API_KEY"):
            assert step["env"][key] == ""
