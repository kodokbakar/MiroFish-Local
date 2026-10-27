"""
Neo4j label validation utilities.
"""
import re
from typing import Any

LABEL_PATTERN = re.compile(r'^[A-Za-z0-9_]+$')


def is_valid_label(label: Any) -> bool:
    """Check if label is a valid Neo4j label (non-empty string matching ^[A-Za-z0-9_]+$)."""
    if not isinstance(label, str):
        return False
    if "\n" in label or "\r" in label:
        return False
    return bool(LABEL_PATTERN.match(label))


def validate_label(label: str) -> str:
    """Validate Neo4j label against ^[A-Za-z0-9_]+$.

    Args:
        label: The label to validate.

    Returns:
        The validated label string.

    Raises:
        ValueError: If label is not a string, is empty, or contains invalid characters.
    """
    if not is_valid_label(label):
        raise ValueError(
            f"Invalid Neo4j label: {label!r}. Labels must match {LABEL_PATTERN.pattern}"
        )
    return label
