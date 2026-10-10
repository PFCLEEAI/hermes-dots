"""§13.5: edits, gated promotion, conflicts, suppression, append-only logs."""

from dataclasses import replace
import json

import pytest

from hermes_dots.brain import Brain
from .invariants import assert_invariants


def test_owner_edits_promotion_conflict_and_forget(scenario):
    s = scenario
    note = s.brain.learn("Owner editable record")
    path = s.brain.writer.path_for(note)
    _, digest = s.brain.writer.read(path)
    owner_text = path.read_bytes() + b"\nOwner: preserve this sentence.\n"
    path.write_bytes(owner_text)
    s.owner_edits[path] = owner_text
    s.brain.writer.write(replace(note, body="New verified addition"), expected_hash=digest)
    assert path.read_bytes().startswith(owner_text) and "Addition by orca" in path.read_text()
    assert any(item["kind"] == "owner_edit_preserved" and item["told_owner"]
               for data in s.logs().values() for item in (json.loads(line) for line in data.decode().splitlines()))
    for authority, text in (("owner_stated", "Acme is ready"), ("verified", "Verified milestone passed")):
        ref = s.brain.record_feedback("correction", text)
        fact = s.brain.learn(text, type="fact", confidence="direct", evidence_refs=[ref])
        assert s.brain.promote(fact.id, **{authority: True})["status"] == "approved"
    inferred = s.brain.learn("Alex prefers concise drafts", type="preference", confidence="inferred")
    assert s.brain.promote(inferred.id, verified=True, owner_stated=True)["status"] == "proposed"
    missing = s.brain.learn("An unverified fact", type="fact", confidence="direct")
    assert s.brain.promote(missing.id, verified=True)["status"] == "proposed"
    conflict = s.brain.learn("Acme is not ready", evidence_refs=["evidence://new-owner-correction"])
    assert conflict.confidence == "conflicting"
    versions = s.brain.digest_items()["conflicts"][0]["versions"]
    assert {version["body"] for version in versions} == {"Acme is ready", "Acme is not ready"}
    s.brain.learn("Acme notebook lesson")
    proposal = s.brain.nightly_reflection([{"text": "Acme feedback"}], lambda _: "Improve Acme answers",
                                         day=s.clock.now().date().isoformat())
    assert proposal.exists()
    s.brain.index.incremental_pass()
    before = s.logs()
    s.brain.forget("Acme")
    s.forgotten.append("Acme")
    assert s.logs() == before and not proposal.exists()
    assert not s.brain.search("Acme")
    with pytest.raises(ValueError, match="Suppressed"):
        s.brain.learn("Acme fact from kept logs")
    s.brain.index.rebuild()
    assert not s.brain.search("Acme")
    assert not Brain(s.root).search("Acme")
    assert s.logs() == before
    assert_invariants(s)
