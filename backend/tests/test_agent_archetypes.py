"""Tests for Issue #77: Custom agent archetypes beyond OASIS default personas."""

import pytest
from app.services.oasis_profile_generator import (
    OasisProfileGenerator,
    AgentArchetype,
    DEFAULT_ARCHETYPES,
)
from app.services.entity_reader import EntityNode


def test_default_archetypes_defined():
    """Built-in archetypes must exist with expected keys and trait specifications."""
    assert "critic" in DEFAULT_ARCHETYPES
    assert "supporter" in DEFAULT_ARCHETYPES
    assert "analyst" in DEFAULT_ARCHETYPES
    assert "influencer" in DEFAULT_ARCHETYPES
    assert "lurker" in DEFAULT_ARCHETYPES

    critic = DEFAULT_ARCHETYPES["critic"]
    assert critic.name == "Critic"
    assert critic.traits
    assert critic.posting_tone


def test_profile_generator_assigns_and_injects_archetype():
    """When archetypes are supplied, profile generator assigns archetype and injects instructions into persona."""
    custom_archetype = AgentArchetype(
        key="skeptic",
        name="Skeptic",
        description="Always questions claims and asks for empirical evidence.",
        traits=["critical", "inquisitive", "cautious"],
        posting_tone="analytical and probing",
        activity_preferences={"LIKE_POST": 0.2, "CREATE_POST": 0.8},
    )

    generator = OasisProfileGenerator(api_key="test-key", archetypes=[custom_archetype])

    entity = EntityNode(
        uuid="e1",
        name="Alice Scientist",
        labels=["Entity", "Person"],
        summary="A senior climate researcher.",
        attributes={"occupation": "Scientist"},
    )

    profile = generator.generate_profile_from_entity(
        entity=entity,
        user_id=1,
        use_llm=False,
        archetype=custom_archetype,
    )

    assert profile.archetype == "Skeptic"
    # Persona should reflect assigned archetype instructions
    assert "Skeptic" in profile.persona or "critical" in profile.persona or "cautious" in profile.persona


def test_batch_generation_distributes_archetypes_by_ratio():
    """Batch profile generation distributes assigned archetypes across entities."""
    generator = OasisProfileGenerator(api_key="test-key")

    entities = [
        EntityNode(uuid=f"e{i}", name=f"Agent_{i}", labels=["Entity", "Person"], summary=f"User {i}", attributes={})
        for i in range(10)
    ]

    archetype_ratios = {
        "critic": 0.5,
        "supporter": 0.5,
    }

    profiles = generator.generate_profile_batch(
        entities=entities,
        parallel_count=2,
        use_llm=False,
        archetype_ratios=archetype_ratios,
    )

    assert len(profiles) == 10
    assigned = [p.archetype for p in profiles if p.archetype]
    assert len(assigned) == 10
    assert "Critic" in assigned
    assert "Supporter" in assigned
