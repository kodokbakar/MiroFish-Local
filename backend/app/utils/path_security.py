"""
Path and identifier security utilities to prevent path traversal.
"""

import os
import re

SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_safe_id(identifier: str) -> str:
    """
    Validate that an identifier contains only safe characters
    (alphanumeric, dash, underscore) and no path traversal sequences.

    Args:
        identifier: The identifier to validate.

    Returns:
        The validated identifier string.

    Raises:
        ValueError: If the identifier is invalid, empty, or contains traversal characters.
    """
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"Invalid identifier: {identifier!r}")

    if not SAFE_ID_REGEX.match(identifier):
        raise ValueError(f"Invalid characters or traversal sequences in identifier: {identifier!r}")

    return identifier


def get_safe_path(base_dir: str, identifier: str) -> str:
    """
    Resolve and validate a child directory/file path within base_dir.

    Ensures that the identifier contains only safe characters and that the
    resolved real path is strictly within the real base directory.

    Args:
        base_dir: Base directory path.
        identifier: Child identifier / directory name.

    Returns:
        The safe path joining base_dir and identifier.

    Raises:
        ValueError: If identifier is invalid or traversal is detected.
    """
    validate_safe_id(identifier)

    target_path = os.path.join(base_dir, identifier)
    base_real = os.path.realpath(base_dir)
    resolved = os.path.realpath(target_path)

    if resolved == base_real:
        raise ValueError(f"Path resolves to root directory: {identifier!r}")

    try:
        common = os.path.commonpath([base_real, resolved])
    except ValueError as e:
        raise ValueError(f"Path traversal detected: {identifier!r}") from e

    if common != base_real:
        raise ValueError(f"Path traversal detected: {identifier!r}")

    prefix = base_real if base_real.endswith(os.sep) else base_real + os.sep
    if not resolved.startswith(prefix):
        raise ValueError(f"Path traversal detected: {identifier!r}")

    return target_path
