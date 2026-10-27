"""
Tests for LLM profile field coercion and normalization (port 010).
"""

import json
import csv
import tempfile
import os
from typing import Dict, Any

import pytest

from app.services.oasis_profile_generator import (
    _coerce_to_str,
    _coerce_to_str_list,
    OasisAgentProfile,
    OasisProfileGenerator,
)


def test_coerce_to_str():
    # Plain str
    assert _coerce_to_str("simple text") == "simple text"
    assert _coerce_to_str("") == ""

    # Dict with recognized keys ('text', 'value', 'description', 'content', 'summary', 'name')
    assert _coerce_to_str({"text": "extracted text"}) == "extracted text"
    assert _coerce_to_str({"value": "extracted value"}) == "extracted value"
    assert _coerce_to_str({"description": "extracted description"}) == "extracted description"
    assert _coerce_to_str({"content": "extracted content"}) == "extracted content"
    assert _coerce_to_str({"summary": "extracted summary"}) == "extracted summary"
    assert _coerce_to_str({"name": "extracted name"}) == "extracted name"

    # Recognized key with non-string value coerces candidate recursively
    assert _coerce_to_str({"text": 123, "value": "extracted value"}) == "123"
    assert _coerce_to_str({"text": {"value": "nested"}}) == "nested"
    non_str_key_dict = {"unknown": 123}
    assert _coerce_to_str(non_str_key_dict) == json.dumps(non_str_key_dict, ensure_ascii=False)

    # Dict with unrecognized keys (JSON dump fallback, preserves unicode)
    dict_val = {"foo": "bar", "count": 42, "tag": "标签"}
    assert _coerce_to_str(dict_val) == json.dumps(dict_val, ensure_ascii=False)

    # List of strings and dicts
    assert _coerce_to_str(["apple", "banana"]) == "apple, banana"
    assert _coerce_to_str([{"text": "tag1"}, {"value": "tag2"}]) == "tag1, tag2"
    assert _coerce_to_str(["first", {"description": "second"}, 99]) == "first, second, 99"
    assert _coerce_to_str([]) == ""

    # Primitive types (int, float, bool)
    assert _coerce_to_str(123) == "123"
    assert _coerce_to_str(45.67) == "45.67"
    assert _coerce_to_str(True) == "True"
    assert _coerce_to_str(False) == "False"
    assert _coerce_to_str(None) == ""


def test_coerce_to_str_list():
    # List of strings
    assert _coerce_to_str_list(["AI", "ML"]) == ["AI", "ML"]
    assert _coerce_to_str_list([]) == []

    # List of mixed types (strings, dicts, ints)
    assert _coerce_to_str_list([{"name": "Robotics"}, "NLP", 42, 3.14]) == ["Robotics", "NLP", "42", "3.14"]
    assert _coerce_to_str_list([{"unknown": 100}, "Test"]) == [json.dumps({"unknown": 100}, ensure_ascii=False), "Test"]

    # Single string
    assert _coerce_to_str_list("AI") == ["AI"]
    assert _coerce_to_str_list("") == []

    # Single dict
    assert _coerce_to_str_list({"text": "Economy"}) == ["Economy"]
    assert _coerce_to_str_list({"unknown": "test"}) == ['{"unknown": "test"}']

    # Primitives coerced to list of string, None returning empty list
    assert _coerce_to_str_list(None) == []
    assert _coerce_to_str_list(123) == ["123"]
    assert _coerce_to_str_list(45.67) == ["45.67"]
    assert _coerce_to_str_list(True) == ["True"]
    assert _coerce_to_str_list(False) == ["False"]


def test_oasis_agent_profile_post_init_normalization():
    # Field normalization for structured LLM outputs on all fields
    profile = OasisAgentProfile(
        user_id=1,
        user_name="user_1",
        name="User One",
        bio={"text": "Structured bio from LLM"},
        persona={"summary": "Structured persona from LLM"},
        country={"value": "Canada"},
        profession={"name": "Software Engineer"},
        gender={"value": "male"},
        interested_topics=[{"name": "Tech"}, "OpenSource"],
    )

    assert profile.bio == "Structured bio from LLM"
    assert profile.persona == "Structured persona from LLM"
    assert profile.country == "Canada"
    assert profile.profession == "Software Engineer"
    assert profile.gender == "male"
    assert profile.interested_topics == ["Tech", "OpenSource"]


def test_oasis_agent_profile_post_init_none_handling():
    # Handling None values appropriately
    profile = OasisAgentProfile(
        user_id=2,
        user_name="user_2",
        name="User Two",
        bio="Plain bio",
        persona="Plain persona",
        country=None,
        profession=None,
        gender=None,
        interested_topics=None,
    )

    assert profile.bio == "Plain bio"
    assert profile.persona == "Plain persona"
    assert profile.country is None
    assert profile.profession is None
    assert profile.gender is None
    assert profile.interested_topics == []


def test_oasis_agent_profile_post_init_single_values_in_topics():
    # Single string or dict in interested_topics coerced to list
    profile_str = OasisAgentProfile(
        user_id=3,
        user_name="user_3",
        name="User Three",
        bio="Bio",
        persona="Persona",
        interested_topics="SingleTopic",
    )
    assert profile_str.interested_topics == ["SingleTopic"]

    profile_dict = OasisAgentProfile(
        user_id=4,
        user_name="user_4",
        name="User Four",
        bio="Bio",
        persona="Persona",
        interested_topics={"name": "TopicDict"},
    )
    assert profile_dict.interested_topics == ["TopicDict"]


def test_save_twitter_csv_with_normalized_profiles():
    generator = OasisProfileGenerator.__new__(OasisProfileGenerator)

    # 1. Profiles with dict bio and persona normalized at construction
    p1 = OasisAgentProfile(
        user_id=0,
        user_name="user_dict",
        name="User Dict",
        bio={"text": "Overwritten dict bio"},
        persona={"description": "Overwritten dict persona"},
    )

    # 2. Profiles with list bio and persona normalized at construction
    p2 = OasisAgentProfile(
        user_id=1,
        user_name="user_list",
        name="User List",
        bio=["tag1", {"value": "tag2"}],
        persona=["desc1", "desc2"],
    )

    # 3. Profiles with None bio and None persona falling back to defaults at construction
    p3 = OasisAgentProfile(
        user_id=2,
        user_name="user_none",
        name="User None",
        bio=None,
        persona=None,
    )

    # 4. Profiles with matching bio and persona, and newlines
    p4 = OasisAgentProfile(
        user_id=3,
        user_name="user_matching",
        name="User Match",
        bio="Same\nBio\rLine",
        persona="Same\nBio\rLine",
    )

    profiles = [p1, p2, p3, p4]

    with tempfile.TemporaryDirectory() as temp_dir:
        csv_path = os.path.join(temp_dir, "profiles.csv")
        generator._save_twitter_csv(profiles, csv_path)

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 4

        # p1: dicts coerced
        assert rows[0]["user_id"] == "0"
        assert rows[0]["name"] == "User Dict"
        assert rows[0]["username"] == "user_dict"
        assert rows[0]["user_char"] == "Overwritten dict bio Overwritten dict persona"
        assert rows[0]["description"] == "Overwritten dict bio"

        # p2: lists coerced
        assert rows[1]["user_id"] == "1"
        assert rows[1]["user_char"] == "tag1, tag2 desc1, desc2"
        assert rows[1]["description"] == "tag1, tag2"

        # p3: None values fallback to name and default persona at construction
        assert rows[2]["user_id"] == "2"
        assert rows[2]["user_char"] == "User None User None is a participant in social discussions."
        assert rows[2]["description"] == "User None"

        # p4: matching persona and bio avoids duplication, newlines stripped
        assert rows[3]["user_id"] == "3"
        assert rows[3]["user_char"] == "Same Bio Line"
        assert rows[3]["description"] == "Same Bio Line"


def test_save_twitter_csv_json_extension_handling():
    generator = OasisProfileGenerator.__new__(OasisProfileGenerator)
    profile = OasisAgentProfile(
        user_id=0,
        user_name="user_ext",
        name="User Ext",
        bio="Bio",
        persona="Persona",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        # Give .json path to verify conversion to .csv
        json_path = os.path.join(temp_dir, "profiles.json")
        generator._save_twitter_csv([profile], json_path)

        expected_csv_path = os.path.join(temp_dir, "profiles.csv")
        assert os.path.exists(expected_csv_path)
        assert not os.path.exists(json_path)


def test_save_reddit_json_with_normalized_profiles():
    generator = OasisProfileGenerator.__new__(OasisProfileGenerator)

    # 1. Profile with structured objects and long bio (>150 chars) normalized at construction
    p1 = OasisAgentProfile(
        user_id=10,
        user_name="reddit_user_1",
        name="Reddit User 1",
        bio="A" * 200,
        persona={"summary": "Structured persona from LLM"},
        country={"name": "Canada"},
        profession={"title": "Data Scientist"},  # Fallback to json dump
        interested_topics={"topic": "Deep Learning"},
    )

    # 2. Profile with None values for optional fields (country fallback to "US")
    p2 = OasisAgentProfile(
        user_id=None,
        user_name="reddit_user_2",
        name="Reddit User 2",
        bio=None,
        persona=None,
        country=None,
        profession=None,
        interested_topics=None,
    )

    # 3. Profile with list of structured topics
    p3 = OasisAgentProfile(
        user_id=12,
        user_name="reddit_user_3",
        name="Reddit User 3",
        bio={"text": "Bio 3"},
        persona="Persona 3",
        interested_topics=[{"name": "AI"}, {"text": "Security"}, "Crypto"],
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        json_path = os.path.join(temp_dir, "profiles.json")
        generator._save_reddit_json([p1, p2, p3], json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data) == 3

        # Item 1: structured objects coerced and bio truncated to 150
        item1 = data[0]
        assert item1["user_id"] == 10
        assert item1["username"] == "reddit_user_1"
        assert item1["name"] == "Reddit User 1"
        assert len(item1["bio"]) == 150
        assert item1["bio"] == ("A" * 150)
        assert item1["persona"] == "Structured persona from LLM"
        assert item1["country"] == "Canada"
        assert json.loads(item1["profession"]) == {"title": "Data Scientist"}
        assert item1["interested_topics"] == ['{"topic": "Deep Learning"}']
        assert item1["karma"] == 1000

        # Item 2: defaults applied (country -> "US", bio -> name, persona -> fallback, user_id -> idx)
        item2 = data[1]
        assert item2["user_id"] == 1  # defaulted to idx
        assert item2["username"] == "reddit_user_2"
        assert item2["name"] == "Reddit User 2"
        assert item2["bio"] == "Reddit User 2"
        assert item2["persona"] == "Reddit User 2 is a participant in social discussions."
        assert item2["country"] == "US"
        assert "profession" not in item2
        assert "interested_topics" not in item2

        # Item 3: list of structured topics coerced
        item3 = data[2]
        assert item3["user_id"] == 12
        assert item3["bio"] == "Bio 3"
        assert item3["interested_topics"] == ["AI", "Security", "Crypto"]
