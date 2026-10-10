"""§13.12: real scanners on generated data and an external private-terms file."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from hermes_dots.security import redact
from .invariants import REPO, assert_invariants


def test_notes_logs_prompts_and_repo_scans(scenario, tmp_path):
    s = scenario
    tokens = ["xoxb-" + "invented-12345", "ya29." + "invented12345", "sk-" + "invented12345",
              "Bearer " + "invented12345"]
    text = "Acme excerpt " + " ".join(tokens)
    note = s.brain.learn(text)
    s.runtime.hooks.post_tool_call(tool_name="read_file", result=text)
    s.model.fake.register(None, {"answer": "Acme"})
    assert s.engine.complete_structured({}, text) == {"answer": "Acme"}
    captures = s.brain.writer.path_for(note).read_text() + str(s.logs()) + json.dumps(s.model.fake.requests)
    assert all(token not in captures for token in tokens)
    assert redact(captures) == captures
    # A throwaway Git tree allows the actual credential scanner to inspect generated data.
    tree = tmp_path / "capture-tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    (tree / "notes-and-prompts.txt").write_text(captures)
    command = [sys.executable, str(REPO / "scripts/scan_credentials.py"), str(tree)]
    clean = subprocess.run(command, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout
    (tree / "planted.txt").write_text(tokens[0])
    planted = subprocess.run(command, capture_output=True, text=True)
    assert planted.returncode == 1 and tokens[0] not in planted.stdout
    (tree / "planted.txt").unlink()
    # Honor supplied owner terms; otherwise exercise env wiring with an invented term.
    env = dict(os.environ)
    if not env.get("HERMES_DOTS_PRIVATE_TERMS"):
        terms = tmp_path / "private-terms.txt"
        terms.write_text("invented-private-" + uuid4().hex + "\n")
        env["HERMES_DOTS_PRIVATE_TERMS"] = str(terms)
    command = [sys.executable, str(REPO / "scripts/scan_personal.py"), str(REPO)]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Personal scan: clean" in result.stdout
    # Positive control uses only an invented term, never copies owner terms.
    term = "invented-private-" + uuid4().hex
    positive_terms = tmp_path / "positive-terms.txt"
    positive_terms.write_text(term + "\n")
    (tree / "planted.txt").write_text(term)
    planted = subprocess.run([sys.executable, str(REPO / "scripts/scan_personal.py"), str(tree)],
                             env={**env, "HERMES_DOTS_PRIVATE_TERMS": str(positive_terms)},
                             capture_output=True, text=True)
    assert planted.returncode == 1 and term not in planted.stdout
    (tree / "planted.txt").unlink()
    assert_invariants(s)
