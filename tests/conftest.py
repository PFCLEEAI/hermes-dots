"""Shared offline fakes and invented practice mail."""

import json
import sys
from pathlib import Path

import pytest

# tests is not a package; expose its fakes package without changing production imports.
sys.path.insert(0, str(Path(__file__).parent))
from fakes import FakeClock, FakeGmail, FakeModel, FakeSlack


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def practice_mail(fixture_dir):
    return json.loads((fixture_dir / "mail.json").read_text(encoding="utf-8"))


@pytest.fixture
def expected_mail(fixture_dir):
    return json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def sent_mail(fixture_dir):
    return json.loads((fixture_dir / "sent.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def fake_model():
    return FakeModel()


@pytest.fixture
def fake_slack():
    return FakeSlack()


@pytest.fixture
def fake_gmail(practice_mail, sent_mail):
    return FakeGmail(practice_mail, sent_mail)
