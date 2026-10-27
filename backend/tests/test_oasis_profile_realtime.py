"""
Tests for realtime OASIS agent profile persistence (Issue #5).
Covers:
- Realtime Twitter export handles heterogeneous entities without dropping profiles.
- Realtime Twitter CSV header matches OASIS schema: user_id,name,username,user_char,description.
- Realtime Reddit JSON export persists valid JSON with required defaults.
- Atomic save behavior (no partial file truncation, temp file cleanup).
"""

import csv
import json
import os
from unittest.mock import patch

import pytest

from app.services.entity_reader import EntityNode
from app.services.oasis_profile_generator import (
    OasisAgentProfile,
    OasisProfileGenerator,
)


@pytest.fixture
def generator():
    gen = object.__new__(OasisProfileGenerator)
    gen.storage = None
    gen.graph_id = None
    return gen


def test_realtime_twitter_export_heterogeneous_entities_no_drop(generator, tmp_path):
    """Verify realtime Twitter export handles heterogeneous entities without dropping profiles.

    Previously, DictWriter extracted fieldnames from profiles_data[0]. When later profiles had
    optional fields (age, gender, mbti) missing from the first profile, writerows() raised
    ValueError, silently dropping the profiles.
    """
    p1 = OasisAgentProfile(
        user_id=0,
        user_name="user_0",
        name="User Zero",
        bio="First bio without optional attributes",
        persona="First persona",
        age=None,
        gender=None,
        mbti=None,
    )
    p2 = OasisAgentProfile(
        user_id=1,
        user_name="user_1",
        name="User One",
        bio="Second bio with optional attributes",
        persona="Second persona",
        age=28,
        gender="female",
        mbti="INTJ",
        country="US",
        profession="Engineer",
    )
    p3 = OasisAgentProfile(
        user_id=2,
        user_name="user_2",
        name="User Two",
        bio="Third bio with different attributes",
        persona="Third persona",
        age=45,
        gender="male",
        country="UK",
    )

    profiles_map = {0: p1, 1: p2, 2: p3}
    generator.generate_profile_from_entity = lambda entity, user_id, use_llm: profiles_map[user_id]

    entities = [
        EntityNode(uuid="e0", name="User Zero", labels=["Person"], summary="", attributes={}),
        EntityNode(uuid="e1", name="User One", labels=["Person"], summary="", attributes={}),
        EntityNode(uuid="e2", name="User Two", labels=["Person"], summary="", attributes={}),
    ]

    csv_path = tmp_path / "twitter_profiles.csv"

    generated = generator.generate_profiles_from_entities(
        entities=entities,
        use_llm=False,
        parallel_count=1,
        realtime_output_path=str(csv_path),
        output_platform="twitter",
    )

    assert len(generated) == 3
    assert csv_path.exists()

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    # Header + 3 profile rows = 4 lines
    assert len(rows) == 4
    usernames = [row[2] for row in rows[1:]]
    assert usernames == ["user_0", "user_1", "user_2"]


def test_realtime_twitter_csv_header_schema(generator, tmp_path):
    """Verify realtime Twitter CSV header matches OASIS schema: user_id,name,username,user_char,description."""
    p1 = OasisAgentProfile(
        user_id=0,
        user_name="alice_123",
        name="Alice Smith",
        bio="Researcher in AI",
        persona="Enthusiastic about neural networks",
    )

    generator.generate_profile_from_entity = lambda entity, user_id, use_llm: p1
    entity = EntityNode(uuid="e0", name="Alice Smith", labels=["Person"], summary="", attributes={})

    csv_path = tmp_path / "twitter_profiles.csv"

    generator.generate_profiles_from_entities(
        entities=[entity],
        use_llm=False,
        parallel_count=1,
        realtime_output_path=str(csv_path),
        output_platform="twitter",
    )

    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        row = next(reader)

    assert header == ["user_id", "name", "username", "user_char", "description"]
    assert row[0] == "0"
    assert row[1] == "Alice Smith"
    assert row[2] == "alice_123"
    assert row[3] == "Researcher in AI Enthusiastic about neural networks"
    assert row[4] == "Researcher in AI"


def test_realtime_reddit_json_export_defaults(generator, tmp_path):
    """Verify realtime Reddit JSON export persists valid JSON with required defaults."""
    p1 = OasisAgentProfile(
        user_id=None,
        user_name="bob_456",
        name="Bob Jones",
        bio="Bob bio",
        persona="Bob persona",
        age=None,
        gender=None,
        mbti=None,
        country=None,
    )

    generator.generate_profile_from_entity = lambda entity, user_id, use_llm: p1
    entity = EntityNode(uuid="e0", name="Bob Jones", labels=["Person"], summary="", attributes={})

    json_path = tmp_path / "reddit_profiles.json"

    generator.generate_profiles_from_entities(
        entities=[entity],
        use_llm=False,
        parallel_count=1,
        realtime_output_path=str(json_path),
        output_platform="reddit",
    )

    assert json_path.exists()
    with json_path.open(encoding="utf-8") as handle:
        data = json.load(handle)

    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    assert item["user_id"] == 0
    assert item["username"] == "bob_456"
    assert item["name"] == "Bob Jones"
    assert item["age"] == 30
    assert item["gender"] == "other"
    assert item["mbti"] == "ISTJ"
    assert item["country"] == "US"
    assert item["karma"] == 1000


def test_atomic_save_twitter_csv_preserves_existing_on_error(generator, tmp_path):
    """Verify atomic save behavior for Twitter CSV: no partial file truncation on write failure."""
    csv_path = tmp_path / "twitter_profiles.csv"
    initial_content = "user_id,name,username,user_char,description\n999,Old,old_user,Old char,Old desc\n"
    csv_path.write_text(initial_content, encoding="utf-8")

    profile = OasisAgentProfile(
        user_id=1,
        user_name="new_user",
        name="New User",
        bio="Bio",
        persona="Persona",
    )

    temp_path = tmp_path / "twitter_profiles.csv.tmp"

    with patch("csv.writer", side_effect=RuntimeError("Disk failure")):
        with pytest.raises(RuntimeError, match="Disk failure"):
            generator._save_twitter_csv([profile], str(csv_path))

    # Original file is preserved without truncation
    assert csv_path.read_text(encoding="utf-8") == initial_content
    # Temporary file was cleaned up
    assert not temp_path.exists()


def test_atomic_save_reddit_json_preserves_existing_on_error(generator, tmp_path):
    """Verify atomic save behavior for Reddit JSON: no partial file truncation on write failure."""
    json_path = tmp_path / "reddit_profiles.json"
    initial_content = '[{"user_id": 999, "username": "old_user"}]'
    json_path.write_text(initial_content, encoding="utf-8")

    profile = OasisAgentProfile(
        user_id=1,
        user_name="new_user",
        name="New User",
        bio="Bio",
        persona="Persona",
    )

    temp_path = tmp_path / "reddit_profiles.json.tmp"

    with patch("json.dump", side_effect=RuntimeError("Serialization failure")):
        with pytest.raises(RuntimeError, match="Serialization failure"):
            generator._save_reddit_json([profile], str(json_path))

    # Original file is preserved without truncation
    assert json_path.read_text(encoding="utf-8") == initial_content
    # Temporary file was cleaned up
    assert not temp_path.exists()


def test_atomic_save_creates_parent_directories(generator, tmp_path):
    """Verify saving creates parent directories automatically if they do not exist."""
    nested_csv = tmp_path / "nested" / "dir" / "profiles.csv"
    nested_json = tmp_path / "nested" / "dir" / "profiles.json"

    profile = OasisAgentProfile(
        user_id=1,
        user_name="user_test",
        name="User Test",
        bio="Bio",
        persona="Persona",
    )

    generator._save_twitter_csv([profile], str(nested_csv))
    assert nested_csv.exists()

    generator._save_reddit_json([profile], str(nested_json))
    assert nested_json.exists()
