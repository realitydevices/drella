from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MARKER = "drella-superstar-run:v1"


def test_the_prompt_exists_starts_with_the_marker_and_fits_a_job_prompt():
    text = (ROOT / "docs" / "superstar.md").read_text()
    assert text.splitlines()[0] == MARKER
    assert len(text.encode()) <= 64 * 1024  # MAX_PROMPT_BYTES in drella/app.py
    assert "<!-- drella-superstar-ledger:v1 -->" in text
    assert "Verdict: approve with nits" in text


def test_the_cronjob_parses_and_is_not_in_the_default_kustomization():
    docs = list(yaml.safe_load_all((ROOT / "deploy" / "superstar-cronjob.yaml").read_text()))
    (cj,) = [d for d in docs if d and d["kind"] == "CronJob"]
    assert cj["spec"]["schedule"] == "*/30 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    script = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "claude-opus" in script and "realitydevices/drella" in script
    kustomization = (ROOT / "deploy" / "kustomization.yaml").read_text()
    assert "superstar-cronjob" not in yaml.safe_load(kustomization).get("resources", [])
