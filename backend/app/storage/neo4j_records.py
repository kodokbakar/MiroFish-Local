"""Convert Neo4j records to the public graph storage representation."""

import json
from typing import Any, Dict, List


def node_to_dict(node: Any, labels: List[str]) -> Dict[str, Any]:
    if isinstance(node, dict):
        props = dict(node)
    elif hasattr(node, "items"):
        props = dict(node.items())
    else:
        props = dict(node)
    attrs_json = props.pop("attributes_json", "{}")
    try:
        attributes = json.loads(attrs_json) if attrs_json else {}
    except (json.JSONDecodeError, TypeError):
        attributes = {}
    res = {
        "uuid": props.get("uuid", ""),
        "name": props.get("name", ""),
        "labels": [label for label in labels if label != "Entity"] if labels else [],
        "summary": props.get("summary", ""),
        "attributes": attributes,
        "created_at": props.get("created_at"),
        "created_round": props.get("created_round", 0),
        "simulation_id": props.get("simulation_id"),
    }
    if "community_id" in props and props["community_id"] is not None:
        res["community_id"] = props["community_id"]
    return res


def edge_to_dict(
    rel: Any, source_uuid: str, target_uuid: str
) -> Dict[str, Any]:
    if isinstance(rel, dict):
        props = dict(rel)
    elif hasattr(rel, "items"):
        props = dict(rel.items())
    else:
        props = dict(rel)
    attrs_json = props.pop("attributes_json", "{}")
    try:
        attributes = json.loads(attrs_json) if attrs_json else {}
    except (json.JSONDecodeError, TypeError):
        attributes = {}
    episode_ids = props.get("episode_ids", [])
    if episode_ids and not isinstance(episode_ids, list):
        episode_ids = [str(episode_ids)]
    return {
        "uuid": props.get("uuid", ""),
        "name": props.get("name", ""),
        "fact": props.get("fact", ""),
        "source_node_uuid": source_uuid,
        "target_node_uuid": target_uuid,
        "attributes": attributes,
        "created_at": props.get("created_at"),
        "valid_at": props.get("valid_at"),
        "invalid_at": props.get("invalid_at"),
        "expired_at": props.get("expired_at"),
        "episode_ids": episode_ids,
        "created_round": props.get("created_round", 0),
        "simulation_id": props.get("simulation_id"),
    }
