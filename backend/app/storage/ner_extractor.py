"""
NER/RE Extractor — entity and relation extraction via local LLM

Replaces Zep Cloud's built-in NER/RE pipeline.
Uses LLMClient.chat_json() with a structured prompt to extract
entities and relations from text chunks, guided by the graph's ontology.
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional

from openai import APIConnectionError, APITimeoutError

from ..config import Config
from ..utils.llm_client import LLMClient
from .extraction_cache import ExtractionCache, is_valid_extraction
from .label_utils import is_valid_label

logger = logging.getLogger('mirofish.ner_extractor')


def _retryable_extraction_error(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return isinstance(error, (ValueError, APIConnectionError, APITimeoutError, TimeoutError, ConnectionError, OSError))

# System prompt template for NER/RE extraction
_SYSTEM_PROMPT = """You are a Named Entity Recognition and Relation Extraction system.
Given a text and an ontology (entity types + relation types), extract all entities and relations.

ONTOLOGY:
{ontology_description}

RULES:
1. Only extract entity types and relation types defined in the ontology.
2. Normalize entity names: strip whitespace, use canonical form (e.g., "Jack Ma" not "ma jack").
3. Each entity must have: name, type (from ontology), and optional attributes.
4. Each relation must have: source entity name, target entity name, type (from ontology), and a fact sentence describing the relationship.
5. If no entities or relations are found, return empty lists.
6. Be precise — only extract what is explicitly stated or strongly implied in the text.

Return ONLY valid JSON in this exact format:
{{
  "entities": [
    {{"name": "...", "type": "...", "attributes": {{"key": "value"}}}}
  ],
  "relations": [
    {{"source": "...", "target": "...", "type": "...", "fact": "..."}}
  ]
}}"""

_USER_PROMPT = """Extract entities and relations from the following text:

{text}"""

EXTRACTION_SCHEMA_VERSION = 1
EXTRACTION_TEMPERATURE = 0.1
EXTRACTION_MAX_TOKENS = 4096


class NERExtractionError(RuntimeError):
    """Raised when the provider cannot produce a valid extraction."""


class NERExtractor:
    """Extract entities and relations from text using local LLM."""

    def __init__(self, llm_client: Optional[LLMClient] = None, max_retries: int = 2,
                 cache_dir: Optional[str | Path] = None):
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self.llm = llm_client or LLMClient(model=Config.get_stage_model("ner"))
        self.max_retries = max_retries
        self.cache = ExtractionCache(cache_dir or Path(Config.UPLOAD_FOLDER) / "ner_cache")

    def _cache_key(self, text: str, ontology: Dict[str, Any]) -> Optional[str]:
        """Hash all effective extraction inputs without persisting credentials or text."""
        base_url = getattr(self.llm, "base_url", None)
        model = getattr(self.llm, "model", None)
        if not isinstance(base_url, str) or not base_url or not isinstance(model, str) or not model:
            # Unknown injected client identity must not reuse another provider's output.
            return None
        payload = {
            "text": text.strip(),
            "ontology": ontology,
            "provider": f"{type(self.llm).__module__}.{type(self.llm).__qualname__}",
            "base_url": base_url,
            "model": model,
            "system_prompt": _SYSTEM_PROMPT,
            "user_prompt": _USER_PROMPT,
            "temperature": EXTRACTION_TEMPERATURE,
            "max_tokens": EXTRACTION_MAX_TOKENS,
            "json_mode": "json_object_with_explicit_unsupported_fallback",
            "ollama_num_ctx": getattr(self.llm, "_num_ctx", None) if "11434" in base_url else None,
            "max_retries": self.max_retries,
            "schema_version": EXTRACTION_SCHEMA_VERSION,
        }
        try:
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def extract(self, text: str, ontology: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract entities and relations from text, guided by ontology.

        Args:
            text: Input text chunk
            ontology: Dict with 'entity_types' and 'relation_types' from graph

        Returns:
            Dict with 'entities' and 'relations' lists:
            {
                "entities": [{"name": str, "type": str, "attributes": dict}],
                "relations": [{"source": str, "target": str, "type": str, "fact": str}]
            }
        """
        if not text or not text.strip():
            return {"entities": [], "relations": []}

        cache_key = self._cache_key(text, ontology)
        if cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        ontology_desc = self._format_ontology(ontology)
        system_msg = _SYSTEM_PROMPT.format(ontology_description=ontology_desc)
        user_msg = _USER_PROMPT.format(text=text.strip())

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        for attempt in range(self.max_retries + 1):
            try:
                result = self.llm.chat_json(
                    messages=messages,
                    temperature=EXTRACTION_TEMPERATURE,
                    max_tokens=EXTRACTION_MAX_TOKENS,
                )
                if not self._valid_raw_result(result):
                    raise ValueError("NER response contains incomplete entities or relations")
                cleaned = self._validate_and_clean(result, ontology)
                if not is_valid_extraction(cleaned):
                    raise ValueError("NER response contains invalid entities or relations")
                if cache_key is not None:
                    self.cache.put(cache_key, cleaned)
                return cleaned
            except Exception as error:
                status = getattr(error, "status_code", None)
                retryable = _retryable_extraction_error(error)
                logger.warning(
                    "NER extraction attempt %d/%d failed: type=%s status=%s retryable=%s",
                    attempt + 1, self.max_retries + 1, type(error).__name__,
                    status if isinstance(status, int) else "unknown", retryable,
                )
                if not retryable or attempt >= self.max_retries:
                    detail = f"HTTP {status}" if isinstance(status, int) else type(error).__name__
                    raise NERExtractionError(f"NER extraction failed ({detail})") from None
                time.sleep(min(2 ** attempt, 4))

    @staticmethod
    def _valid_raw_result(result: Any) -> bool:
        """Reject malformed records before normalization can discard them."""
        if not isinstance(result, dict):
            return False
        entities = result.get("entities")
        relations = result.get("relations")
        if not isinstance(entities, list) or not isinstance(relations, list):
            return False
        for entity in entities:
            if not isinstance(entity, dict):
                return False
            if not all(isinstance(entity.get(key), str) and entity[key].strip() for key in ("name", "type")):
                return False
        for relation in relations:
            if not isinstance(relation, dict):
                return False
            if not all(isinstance(relation.get(key), str) and relation[key].strip() for key in ("source", "target", "type", "fact")):
                return False
        return True


    def _format_ontology(self, ontology: Dict[str, Any]) -> str:
        """Format ontology dict into readable text for the LLM prompt."""
        parts = []

        entity_types = ontology.get("entity_types", [])
        if entity_types:
            parts.append("Entity Types:")
            for et in entity_types:
                if isinstance(et, dict):
                    name = et.get("name", str(et))
                    desc = et.get("description", "")
                    attrs = et.get("attributes", [])
                    line = f"  - {name}"
                    if desc:
                        line += f": {desc}"
                    if attrs:
                        attr_names = [a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in attrs]
                        line += f" (attributes: {', '.join(attr_names)})"
                    parts.append(line)
                else:
                    parts.append(f"  - {et}")

        relation_types = ontology.get("relation_types", ontology.get("edge_types", []))
        if relation_types:
            parts.append("\nRelation Types:")
            for rt in relation_types:
                if isinstance(rt, dict):
                    name = rt.get("name", str(rt))
                    desc = rt.get("description", "")
                    source_targets = rt.get("source_targets", [])
                    line = f"  - {name}"
                    if desc:
                        line += f": {desc}"
                    if source_targets:
                        st_strs = [f"{st.get('source', '?')} → {st.get('target', '?')}" for st in source_targets]
                        line += f" ({', '.join(st_strs)})"
                    parts.append(line)
                else:
                    parts.append(f"  - {rt}")

        if not parts:
            parts.append("No specific ontology defined. Extract all entities and relations you find.")

        return "\n".join(parts)

    def _validate_and_clean(
        self, result: Dict[str, Any], ontology: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate and normalize LLM output."""
        entities = result.get("entities", [])
        relations = result.get("relations", [])

        # Get valid type names from ontology
        valid_entity_types = set()
        for et in ontology.get("entity_types", []):
            if isinstance(et, dict):
                valid_entity_types.add(et.get("name", "").strip())
            else:
                valid_entity_types.add(str(et).strip())

        valid_relation_types = set()
        for rt in ontology.get("relation_types", ontology.get("edge_types", [])):
            if isinstance(rt, dict):
                valid_relation_types.add(rt.get("name", "").strip())
            else:
                valid_relation_types.add(str(rt).strip())

        # Clean entities
        cleaned_entities = []
        seen_names = set()
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name", "")).strip()
            etype = str(entity.get("type", "Entity")).strip()
            if not name:
                continue

            # Deduplicate by normalized name
            name_lower = name.lower()
            if name_lower in seen_names:
                continue
            seen_names.add(name_lower)

            # Validate entity type label to prevent Cypher injection
            if not is_valid_label(etype):
                logger.warning(
                    f"Entity '{name}' has invalid label '{etype}', falling back to 'Entity'"
                )
                etype = "Entity"
            elif valid_entity_types and etype not in valid_entity_types:
                logger.debug(f"Entity '{name}' has type '{etype}' not in ontology, keeping anyway")
            cleaned_entities.append({
                "name": name,
                "type": etype,
                "attributes": entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {},
            })

        # Clean relations
        cleaned_relations = []
        entity_names_lower = {e["name"].lower() for e in cleaned_entities}
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            source = str(relation.get("source", "")).strip()
            target = str(relation.get("target", "")).strip()
            rtype = str(relation.get("type", "RELATED_TO")).strip()
            fact = str(relation.get("fact", "")).strip()

            if not source or not target:
                continue

            # Ensure source and target entities exist
            # (they might not if LLM hallucinated a relation without the entity)
            if source.lower() not in entity_names_lower:
                cleaned_entities.append({
                    "name": source,
                    "type": "Entity",
                    "attributes": {},
                })
                entity_names_lower.add(source.lower())

            if target.lower() not in entity_names_lower:
                cleaned_entities.append({
                    "name": target,
                    "type": "Entity",
                    "attributes": {},
                })
                entity_names_lower.add(target.lower())

            cleaned_relations.append({
                "source": source,
                "target": target,
                "type": rtype,
                "fact": fact or f"{source} {rtype} {target}",
            })

        return {
            "entities": cleaned_entities,
            "relations": cleaned_relations,
        }
