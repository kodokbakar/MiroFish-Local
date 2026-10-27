import csv
import json

from app.services.oasis_profile_generator import (
    OasisAgentProfile,
    OasisProfileGenerator,
    _coerce_to_str,
    _coerce_to_str_list,
)


def test_text_coercion_handles_none_nested_objects_and_lists():
    assert _coerce_to_str(None) == ""
    assert _coerce_to_str({"text": {"value": "中文"}}) == "中文"
    assert _coerce_to_str([None, {"description": "alpha"}, ["beta"]]) == "alpha, beta"
    assert _coerce_to_str({"unexpected": None}) == '{"unexpected": null}'


def test_list_coercion_flattens_nested_values_and_drops_missing_items():
    assert _coerce_to_str_list(
        ["AI", ["policy", None], {"name": "society"}, 4]
    ) == ["AI", "policy", "society", "4"]
    assert _coerce_to_str_list(None) == []


def test_profile_construction_is_the_single_normalization_boundary():
    profile = OasisAgentProfile(
        user_id=1,
        user_name="agent",
        name="Agent Name",
        bio=None,
        persona={"text": {"value": "详细人设"}},
        gender=["female"],
        mbti={"value": "INTJ"},
        country={"name": "中国"},
        profession={"description": "研究员"},
        interested_topics=["AI", ["政策", None], {"name": "社会"}],
    )

    assert profile.bio == "Agent Name"
    assert profile.persona == "详细人设"
    assert profile.gender == "female"
    assert profile.mbti == "INTJ"
    assert profile.country == "中国"
    assert profile.profession == "研究员"
    assert profile.interested_topics == ["AI", "政策", "社会"]
    assert "None" not in json.dumps(profile.to_dict(), ensure_ascii=False)


def test_normalized_profile_serializes_to_twitter_and_reddit(tmp_path):
    profile = OasisAgentProfile(
        user_id=1,
        user_name="agent",
        name="Agent Name",
        bio={"summary": "公开简介"},
        persona=["详细", "人设"],
        mbti={"text": "ENFP"},
        interested_topics=["AI", ["政策"]],
    )
    generator = object.__new__(OasisProfileGenerator)
    twitter_path = tmp_path / "twitter_profiles.csv"
    reddit_path = tmp_path / "reddit_profiles.json"

    generator._save_twitter_csv([profile], str(twitter_path))
    generator._save_reddit_json([profile], str(reddit_path))

    with twitter_path.open(encoding="utf-8") as handle:
        twitter = next(csv.DictReader(handle))
    reddit = json.loads(reddit_path.read_text(encoding="utf-8"))[0]

    assert twitter["description"] == "公开简介"
    assert twitter["user_char"] == "公开简介 详细, 人设"
    assert reddit["bio"] == "公开简介"
    assert reddit["persona"] == "详细, 人设"
    assert reddit["mbti"] == "ENFP"
    assert reddit["interested_topics"] == ["AI", "政策"]


def test_coerce_to_str_deeply_nested_and_primitives():
    # Deeply nested tuples and lists mixing empty strings, None, numbers, booleans
    assert _coerce_to_str((1, (True, None, ""), ("deep", False))) == "1, True, deep, False"
    assert _coerce_to_str([None, "", ((), [None, ""])]) == ""
    assert _coerce_to_str((42, 3.14, True, False)) == "42, 3.14, True, False"


def test_coerce_to_str_list_deeply_nested_and_primitives():
    # Deeply nested tuples and lists with None, empty strings, numbers, booleans
    assert _coerce_to_str_list(
        [(), (None, "", [42, False, (True, "deep")])]
    ) == ["42", "False", "True", "deep"]
    assert _coerce_to_str_list((1, 2, (3, [4, None]))) == ["1", "2", "3", "4"]
    assert _coerce_to_str_list([None, "", ([], ()), {}]) == ["{}"]
    assert _coerce_to_str_list(True) == ["True"]
    assert _coerce_to_str_list(0) == ["0"]
    assert _coerce_to_str_list("") == []


def test_coerce_to_str_dict_candidate_precedence_and_empty_fallbacks():
    # Candidate precedence ('text', 'value', 'description', 'content', 'summary', 'name')
    dict_multi = {"name": "NameVal", "text": "TextVal", "description": "DescVal"}
    assert _coerce_to_str(dict_multi) == "TextVal"

    # Empty candidate values fall through to next candidate key
    dict_empty_fallthrough = {"text": "", "value": None, "summary": "FoundSummary"}
    assert _coerce_to_str(dict_empty_fallthrough) == "FoundSummary"

    # All candidate keys empty falls through to json dumps
    dict_all_empty = {"text": "", "value": None, "name": ""}
    assert _coerce_to_str(dict_all_empty) == '{"text": "", "value": null, "name": ""}'

    # Dict without candidate keys dumps json
    assert _coerce_to_str({"role": "admin", "score": 100}) == '{"role": "admin", "score": 100}'


def test_oasis_agent_profile_default_and_empty_optional_fields():
    # Minimal profile with default values
    profile_defaults = OasisAgentProfile(
        user_id=1,
        user_name="alice",
        name="Alice",
        bio="Alice bio",
        persona="Alice persona",
    )
    assert profile_defaults.karma == 1000
    assert profile_defaults.friend_count == 100
    assert profile_defaults.follower_count == 150
    assert profile_defaults.statuses_count == 500
    assert profile_defaults.age is None
    assert profile_defaults.gender is None
    assert profile_defaults.mbti is None
    assert profile_defaults.country is None
    assert profile_defaults.profession is None
    assert profile_defaults.interested_topics == []
    assert profile_defaults.source_entity_uuid is None
    assert profile_defaults.source_entity_type is None

    # Profile initialized with empty/falsy optional fields
    profile_empty = OasisAgentProfile(
        user_id=2,
        user_name="bob",
        name="Bob",
        bio="",
        persona="",
        gender="",
        mbti="",
        country="",
        profession="",
        interested_topics=[],
    )
    assert profile_empty.bio == "Bob"
    assert profile_empty.persona == "Bob is a participant in social discussions."
    assert profile_empty.gender is None
    assert profile_empty.mbti is None
    assert profile_empty.country is None
    assert profile_empty.profession is None
    assert profile_empty.interested_topics == []

    # Optional fields omitted in platform export dicts when None/empty
    reddit_fmt = profile_empty.to_reddit_format()
    assert "gender" not in reddit_fmt
    assert "mbti" not in reddit_fmt
    assert "country" not in reddit_fmt
    assert "profession" not in reddit_fmt
    assert "interested_topics" not in reddit_fmt

    twitter_fmt = profile_empty.to_twitter_format()
    assert "gender" not in twitter_fmt
    assert "mbti" not in twitter_fmt
    assert "country" not in twitter_fmt
    assert "profession" not in twitter_fmt
    assert "interested_topics" not in twitter_fmt


def test_reddit_and_twitter_export_formatting_edge_cases(tmp_path):
    generator = object.__new__(OasisProfileGenerator)

    # 1. Twitter CSV newline sanitization and matching bio/persona
    profile_newlines = OasisAgentProfile(
        user_id=1,
        user_name="multiline_user",
        name="Multiline User",
        bio="Line 1\r\nLine 2\nLine 3",
        persona="Line 1\r\nLine 2\nLine 3",
    )
    # File path ending in .json gets replaced to .csv
    csv_target = tmp_path / "profiles_export.json"
    generator._save_twitter_csv([profile_newlines], str(csv_target))

    expected_csv_path = tmp_path / "profiles_export.csv"
    assert expected_csv_path.exists()

    with expected_csv_path.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert "\n" not in row["user_char"] and "\r" not in row["user_char"]
    assert row["user_char"] == "Line 1  Line 2 Line 3"
    assert row["description"] == "Line 1  Line 2 Line 3"

    # 2. Reddit JSON edge cases: long bio truncation, user_id=None fallback, gender normalization
    profile_edge = OasisAgentProfile(
        user_id=None,
        user_name="edge_user",
        name="Edge User",
        bio="X" * 200,
        persona="Edge persona",
        gender=" FEMALE ",
        age=None,
        mbti=None,
        country=None,
    )
    json_target = tmp_path / "reddit_export.json"
    generator._save_reddit_json([profile_edge], str(json_target))

    reddit_data = json.loads(json_target.read_text(encoding="utf-8"))[0]
    assert reddit_data["user_id"] == 0  # Fallback to index
    assert len(reddit_data["bio"]) == 150
    assert reddit_data["bio"] == "X" * 150
    assert reddit_data["gender"] == "female"
    assert reddit_data["age"] == 30
    assert reddit_data["mbti"] == "ISTJ"
    assert reddit_data["country"] == "US"
    assert "profession" not in reddit_data
    assert "interested_topics" not in reddit_data
