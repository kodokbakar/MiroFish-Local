"""
Tests for Neo4jStorage ingestion fixes (Issue #1):
1. Attribute data loss prevention in _merge_entity(): preserving and merging attributes across chunks.
2. Relation deduplication and episode_ids accumulation in _create_relation(): MERGE instead of CREATE.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from app.storage.neo4j_storage import Neo4jStorage


class FakeRecord:
    """Mock Neo4j record supporting dict-like access and .get()."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeResult:
    """Mock Neo4j result supporting .single() and iteration."""

    def __init__(self, records=None):
        self._records = [FakeRecord(r) if isinstance(r, dict) else r for r in (records or [])]

    def single(self):
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class InMemoryNeo4jDriver:
    """
    Simulated Neo4j driver providing an in-memory graph store for testing
    entity upserting, attribute merging, relation deduplication, and episode accumulation.
    """

    def __init__(self):
        self.entities = {}  # (gid, name_lower) -> dict
        self.relations = {}  # (src_uuid, tgt_uuid, gid, name) -> dict
        self.episodes = []
        self.queries = []  # records (cypher_str, params_dict)

    class FakeTx:
        def __init__(self, db):
            self.db = db

        def run(self, query: str, **kwargs):
            cleaned_query = query.strip()
            self.db.queries.append((cleaned_query, kwargs))

            # Ontology check in get_ontology()
            if "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj" in cleaned_query:
                return FakeResult([])

            # Schema queries in _ensure_schema()
            if any(cleaned_query.startswith(prefix) for prefix in ("CREATE CONSTRAINT", "CREATE VECTOR INDEX", "CREATE FULLTEXT INDEX")):
                return FakeResult([])

            # Episode creation
            if "CREATE (ep:Episode" in cleaned_query:
                self.db.episodes.append(kwargs)
                return FakeResult([])

            # Entity attribute retrieval (check existing attributes before upsert)
            if "WHERE n.name_lower IN $name_lowers" in cleaned_query and "RETURN n.name_lower AS name_lower, n.attributes_json AS attributes_json" in cleaned_query:
                results = []
                for nl in kwargs.get("name_lowers", []):
                    key = (kwargs["gid"], nl)
                    if key in self.db.entities:
                        results.append({
                            "name_lower": nl,
                            "attributes_json": self.db.entities[key].get("attributes_json"),
                        })
                return FakeResult(results)

            if "MATCH (n:Entity {graph_id: $gid, name_lower: $name_lower})" in cleaned_query and "RETURN n.attributes_json AS attributes_json" in cleaned_query:
                key = (kwargs["gid"], kwargs["name_lower"])
                if key in self.db.entities:
                    return FakeResult([{"attributes_json": self.db.entities[key].get("attributes_json")}])
                return FakeResult([])

            # Entity upsert (UNWIND or single MERGE)
            if "UNWIND $entities AS e" in cleaned_query:
                results = []
                gid = kwargs["gid"]
                now = kwargs.get("now", "")
                for e in kwargs.get("entities", []):
                    nl = e["name_lower"]
                    key = (gid, nl)
                    if key in self.db.entities:
                        ent = self.db.entities[key]
                        if not ent.get("summary"):
                            ent["summary"] = e.get("summary", "")
                        ent["attributes_json"] = e.get("attrs_json", "{}")
                        ent["embedding"] = e.get("embedding", [])
                    else:
                        ent = {
                            "uuid": e["uuid"],
                            "name": e["name"],
                            "name_lower": nl,
                            "summary": e.get("summary", ""),
                            "attributes_json": e.get("attrs_json", "{}"),
                            "embedding": e.get("embedding", []),
                            "created_at": now,
                            "labels": ["Entity"],
                        }
                        self.db.entities[key] = ent
                    results.append({"name_lower": nl, "uuid": self.db.entities[key]["uuid"]})
                return FakeResult(results)

            if "MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower})" in cleaned_query:
                key = (kwargs["gid"], kwargs["name_lower"])
                if key in self.db.entities:
                    ent = self.db.entities[key]
                    if not ent.get("summary"):
                        ent["summary"] = kwargs.get("summary", "")
                    ent["attributes_json"] = kwargs.get("attrs_json", "{}")
                    ent["embedding"] = kwargs.get("embedding", [])
                else:
                    ent = {
                        "uuid": kwargs["uuid"],
                        "name": kwargs["name"],
                        "name_lower": kwargs["name_lower"],
                        "summary": kwargs.get("summary", ""),
                        "attributes_json": kwargs.get("attrs_json", "{}"),
                        "embedding": kwargs.get("embedding", []),
                        "created_at": kwargs.get("now", ""),
                        "labels": ["Entity"],
                    }
                    self.db.entities[key] = ent
                return FakeResult([{"uuid": self.db.entities[key]["uuid"]}])

            # Label assignment
            if "SET n:`" in cleaned_query:
                label = cleaned_query.split("SET n:`")[1].split("`")[0]
                names = kwargs.get("names")
                if names is None:
                    names = [kwargs.get("nl")]
                for nl in names:
                    if nl:
                        key = (kwargs["gid"], nl)
                        if key in self.db.entities:
                            labels = self.db.entities[key].setdefault("labels", ["Entity"])
                            if label not in labels:
                                labels.append(label)
                return FakeResult([])

            # Missing entity lookup for relations
            if "WHERE n.name_lower IN $missing_names" in cleaned_query and "RETURN n.name_lower AS name_lower, n.uuid AS uuid" in cleaned_query:
                results = []
                for nl in kwargs.get("missing_names", []):
                    key = (kwargs["gid"], nl)
                    if key in self.db.entities:
                        results.append({"name_lower": nl, "uuid": self.db.entities[key]["uuid"]})
                return FakeResult(results)

            # Relation upsert (UNWIND or single MERGE)
            if "UNWIND $relations AS rel_data" in cleaned_query:
                gid = kwargs["gid"]
                ep_id = kwargs["episode_id"]
                now = kwargs.get("now", "")
                for r in kwargs.get("relations", []):
                    src_uuid = r["source_uuid"]
                    tgt_uuid = r["target_uuid"]
                    name = r["name"]
                    key = (src_uuid, tgt_uuid, gid, name)

                    if key in self.db.relations:
                        rel = self.db.relations[key]
                        current_episodes = rel.get("episode_ids")
                        if current_episodes is None:
                            rel["episode_ids"] = [ep_id]
                        elif ep_id not in current_episodes:
                            rel["episode_ids"].append(ep_id)

                        if not rel.get("fact"):
                            rel["fact"] = r.get("fact", "")
                            rel["fact_embedding"] = r.get("fact_embedding", [])
                    else:
                        rel = {
                            "uuid": r["uuid"],
                            "graph_id": gid,
                            "name": name,
                            "source_uuid": src_uuid,
                            "target_uuid": tgt_uuid,
                            "fact": r.get("fact", ""),
                            "fact_embedding": r.get("fact_embedding", []),
                            "attributes_json": "{}",
                            "episode_ids": [ep_id],
                            "created_at": now,
                            "valid_at": None,
                            "invalid_at": None,
                            "expired_at": None,
                        }
                        self.db.relations[key] = rel
                return FakeResult([])

            if "MERGE (src)-[r:RELATION {graph_id: $gid, name: $name}]->(tgt)" in cleaned_query:
                src_uuid = kwargs["src_uuid"]
                tgt_uuid = kwargs["tgt_uuid"]
                gid = kwargs["gid"]
                name = kwargs["name"]
                key = (src_uuid, tgt_uuid, gid, name)

                if key in self.db.relations:
                    rel = self.db.relations[key]
                    ep_id = kwargs["episode_id"]
                    # Accumulate episode_id avoiding duplicates
                    current_episodes = rel.get("episode_ids")
                    if current_episodes is None:
                        rel["episode_ids"] = [ep_id]
                    elif ep_id not in current_episodes:
                        rel["episode_ids"].append(ep_id)

                    # Update fact and fact_embedding if existing fact is empty
                    if not rel.get("fact"):
                        rel["fact"] = kwargs.get("fact", "")
                        rel["fact_embedding"] = kwargs.get("fact_embedding", [])
                else:
                    rel = {
                        "uuid": kwargs["uuid"],
                        "graph_id": gid,
                        "name": name,
                        "source_uuid": src_uuid,
                        "target_uuid": tgt_uuid,
                        "fact": kwargs.get("fact", ""),
                        "fact_embedding": kwargs.get("fact_embedding", []),
                        "attributes_json": "{}",
                        "episode_ids": [kwargs["episode_id"]],
                        "created_at": kwargs.get("now", ""),
                        "valid_at": None,
                        "invalid_at": None,
                        "expired_at": None,
                    }
                    self.db.relations[key] = rel
                return FakeResult([])

            # Node batch retrieval
            if "MATCH (n:Entity)" in cleaned_query and "WHERE n.uuid IN $uuids" in cleaned_query:
                results = []
                uuids = set(kwargs.get("uuids", []))
                for ent in self.db.entities.values():
                    if ent.get("uuid") in uuids:
                        results.append({
                            "n": ent,
                            "labels": ent.get("labels", ["Entity"]),
                        })
                return FakeResult(results)

            return FakeResult([])

    def session(self):
        db = self

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def run(self, query: str, **kwargs):
                return db.FakeTx(db).run(query, **kwargs)

            def execute_write(self, fn, *args, **kwargs):
                return fn(db.FakeTx(db), *args, **kwargs)

            def execute_read(self, fn, *args, **kwargs):
                return fn(db.FakeTx(db), *args, **kwargs)

        return FakeSession()

    def close(self):
        pass


class TestNeo4jStorageIngestion(unittest.TestCase):
    """Test suite for Issue #1: attribute preservation and relation deduplication."""

    def setUp(self):
        self.fake_driver = InMemoryNeo4jDriver()
        self.mock_embedding = MagicMock()
        self.mock_embedding.embed_batch.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
        self.mock_ner = MagicMock()

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=self.fake_driver):
            self.storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=self.mock_embedding,
                ner_extractor=self.mock_ner,
            )

    def test_merge_entity_initial_creation_attributes(self):
        """Verify initial entity creation saves attributes properly."""
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {"role": "Engineer", "dept": "AI"}}],
            "relations": [],
        }

        self.storage.add_text("test-graph", "Alice is an AI Engineer.")

        key = ("test-graph", "alice")
        self.assertIn(key, self.fake_driver.entities)
        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        self.assertEqual(attrs, {"role": "Engineer", "dept": "AI"})

    def test_merge_entity_preserves_attributes_when_second_chunk_has_empty_attrs(self):
        """Verify Issue #1: subsequent chunk with empty attributes does not overwrite existing attributes."""
        # Chunk 1: Alice with rich attributes
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {"role": "Engineer", "dept": "AI", "level": "Senior"}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice is a Senior AI Engineer.")

        # Chunk 2: Alice mentioned again, but NER extraction finds no attributes
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice attended the meeting.")

        key = ("test-graph", "alice")
        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        self.assertEqual(attrs, {"role": "Engineer", "dept": "AI", "level": "Senior"})

    def test_merge_entity_merges_partial_attributes(self):
        """Verify subsequent chunk with partial attributes merges with existing ({**existing, **new})."""
        # Chunk 1: Alice with initial attributes
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {"role": "Engineer", "location": "NYC"}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice is an engineer in NYC.")

        # Chunk 2: Alice with updated role and new team attribute
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {"role": "Tech Lead", "team": "Search"}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice became Tech Lead of Search.")

        key = ("test-graph", "alice")
        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        # 'location' preserved, 'role' updated, 'team' added
        self.assertEqual(
            attrs,
            {"role": "Tech Lead", "location": "NYC", "team": "Search"}
        )

    def test_merge_entity_handles_corrupt_existing_attributes_json(self):
        """Verify recovery when existing node has corrupt or non-dict attributes_json."""
        key = ("test-graph", "corrupt node")
        self.fake_driver.entities[key] = {
            "uuid": "corrupt-uuid",
            "name": "Corrupt Node",
            "name_lower": "corrupt node",
            "summary": "Existing corrupt",
            "attributes_json": "{not-valid-json",
            "embedding": [],
            "created_at": "2026-01-01",
            "labels": ["Entity"],
        }

        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Corrupt Node", "type": "Item", "attributes": {"status": "repaired"}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Mentioning corrupt node.")

        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        self.assertEqual(attrs, {"status": "repaired"})

    def test_create_relation_cypher_structure_and_clauses(self):
        """Verify _create_relation uses MERGE with ON CREATE and ON MATCH clauses per Issue #1 spec."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "TechCorp", "type": "Company", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "TechCorp", "type": "WORKS_AT", "fact": "Alice works at TechCorp"},
            ],
        }

        self.storage.add_text("test-graph", "Alice works at TechCorp.")

        # Inspect queries recorded
        relation_queries = [
            (q, params) for q, params in self.fake_driver.queries
            if "MERGE (src)-[r:RELATION" in q
        ]
        self.assertEqual(len(relation_queries), 1)
        cypher, params = relation_queries[0]

        # Verify UNWIND batching and MERGE pattern specifies graph_id and name
        self.assertIn("UNWIND $relations AS rel_data", cypher)
        self.assertIn("MERGE (src)-[r:RELATION {graph_id: $gid, name: rel_data.name}]->(tgt)", cypher)

        # Verify ON CREATE SET sets required fields
        self.assertIn("ON CREATE SET", cypher)
        self.assertIn("r.uuid = rel_data.uuid", cypher)
        self.assertIn("r.graph_id = $gid", cypher)
        self.assertIn("r.name = rel_data.name", cypher)
        self.assertIn("r.fact = rel_data.fact", cypher)
        self.assertIn("r.fact_embedding = rel_data.fact_embedding", cypher)
        self.assertIn("r.attributes_json = '{}'", cypher)
        self.assertIn("r.episode_ids = [$episode_id]", cypher)
        self.assertIn("r.created_at = $now", cypher)
        self.assertIn("r.valid_at = null", cypher)
        self.assertIn("r.invalid_at = null", cypher)
        self.assertIn("r.expired_at = null", cypher)

        # Verify ON MATCH SET accumulates episode_ids avoiding duplicates
        self.assertIn("ON MATCH SET", cypher)
        self.assertIn("WHEN r.episode_ids IS NULL THEN [$episode_id]", cypher)
        self.assertIn("WHEN $episode_id IN r.episode_ids THEN r.episode_ids", cypher)
        self.assertIn("ELSE r.episode_ids + [$episode_id]", cypher)

        # Verify ON MATCH SET updates fact/fact_embedding if existing fact is empty
        self.assertIn("WHEN r.fact = '' OR r.fact IS NULL THEN rel_data.fact_embedding", cypher)
        self.assertIn("WHEN r.fact = '' OR r.fact IS NULL THEN rel_data.fact", cypher)

        # Verify parameters passed
        self.assertEqual(params["gid"], "test-graph")
        self.assertEqual(len(params["relations"]), 1)
        self.assertEqual(params["relations"][0]["name"], "WORKS_AT")
        self.assertEqual(params["relations"][0]["fact"], "Alice works at TechCorp")

    def test_relation_deduplication_and_episode_ids_accumulation(self):
        """Verify relations between same source and target with same name are deduplicated and episode_ids accumulate."""
        # Chunk 1: Initial relation with empty fact
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {"role": "Engineer"}},
                {"name": "TechCorp", "type": "Company", "attributes": {"industry": "Tech"}},
            ],
            "relations": [
                {"source": "Alice", "target": "TechCorp", "type": "WORKS_AT", "fact": ""},
            ],
        }
        ep1 = self.storage.add_text("test-graph", "Chunk 1: Alice and TechCorp.")

        # Chunk 2: Same relation across second chunk, provides detailed fact
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "TechCorp", "type": "Company", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "TechCorp", "type": "WORKS_AT", "fact": "Alice is employed by TechCorp as an engineer"},
            ],
        }
        ep2 = self.storage.add_text("test-graph", "Chunk 2: Alice works at TechCorp.")

        # Chunk 3: Same relation across third chunk
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "TechCorp", "type": "Company", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "TechCorp", "type": "WORKS_AT", "fact": "Alice works at TechCorp on AI"},
            ],
        }
        ep3 = self.storage.add_text("test-graph", "Chunk 3: Alice continues at TechCorp.")

        # Verify only 1 relationship was created (deduplicated)
        self.assertEqual(len(self.fake_driver.relations), 1)

        rel = list(self.fake_driver.relations.values())[0]
        self.assertEqual(rel["name"], "WORKS_AT")
        self.assertEqual(rel["graph_id"], "test-graph")

        # Verify episode_ids accumulated in order without duplicates
        self.assertEqual(rel["episode_ids"], [ep1, ep2, ep3])

        # Verify fact was updated from empty in chunk 1 to fact from chunk 2, and kept
        self.assertEqual(rel["fact"], "Alice is employed by TechCorp as an engineer")

    def test_relation_reprocessing_same_episode_does_not_duplicate_id(self):
        """Verify re-evaluating relation with the same episode_id does not duplicate it in episode_ids."""
        alice_uuid = "alice-uuid"
        tech_uuid = "tech-uuid"
        rel_key = (alice_uuid, tech_uuid, "test-graph", "WORKS_AT")
        self.fake_driver.relations[rel_key] = {
            "uuid": "rel-uuid",
            "graph_id": "test-graph",
            "name": "WORKS_AT",
            "source_uuid": alice_uuid,
            "target_uuid": tech_uuid,
            "fact": "Existing fact",
            "fact_embedding": [0.1],
            "attributes_json": "{}",
            "episode_ids": ["ep-1", "ep-2"],
            "created_at": "2026-01-01",
            "valid_at": None,
            "invalid_at": None,
            "expired_at": None,
        }

        # Simulate ON MATCH query with existing ep-2
        tx = self.fake_driver.FakeTx(self.fake_driver)
        tx.run(
            """
            MATCH (src:Entity {uuid: $src_uuid})
            MATCH (tgt:Entity {uuid: $tgt_uuid})
            MERGE (src)-[r:RELATION {graph_id: $gid, name: $name}]->(tgt)
            ON MATCH SET r.episode_ids = ...
            """,
            src_uuid=alice_uuid,
            tgt_uuid=tech_uuid,
            gid="test-graph",
            name="WORKS_AT",
            episode_id="ep-2",
            fact="New fact",
            fact_embedding=[0.2],
        )

        rel = self.fake_driver.relations[rel_key]
        self.assertEqual(rel["episode_ids"], ["ep-1", "ep-2"])
        # Non-empty fact not overwritten
        self.assertEqual(rel["fact"], "Existing fact")

    def test_relation_on_match_handles_null_existing_episode_ids(self):
        """Verify ON MATCH handles legacy relation where episode_ids is None."""
        alice_uuid = "alice-uuid"
        tech_uuid = "tech-uuid"
        rel_key = (alice_uuid, tech_uuid, "test-graph", "COLLABORATES")
        self.fake_driver.relations[rel_key] = {
            "uuid": "rel-uuid-2",
            "graph_id": "test-graph",
            "name": "COLLABORATES",
            "source_uuid": alice_uuid,
            "target_uuid": tech_uuid,
            "fact": "",
            "fact_embedding": [],
            "attributes_json": "{}",
            "episode_ids": None,
            "created_at": "2026-01-01",
            "valid_at": None,
            "invalid_at": None,
            "expired_at": None,
        }

        tx = self.fake_driver.FakeTx(self.fake_driver)
        tx.run(
            """
            MERGE (src)-[r:RELATION {graph_id: $gid, name: $name}]->(tgt)
            """,
            src_uuid=alice_uuid,
            tgt_uuid=tech_uuid,
            gid="test-graph",
            name="COLLABORATES",
            episode_id="ep-new",
            fact="They collaborate closely",
            fact_embedding=[0.5],
        )

        rel = self.fake_driver.relations[rel_key]
        self.assertEqual(rel["episode_ids"], ["ep-new"])
        self.assertEqual(rel["fact"], "They collaborate closely")

    def test_merge_entity_nested_and_complex_attributes(self):
        """Verify merging preserves complex types (nested dicts, lists) across chunks."""
        # Chunk 1: complex nested attributes
        self.mock_ner.extract.return_value = {
            "entities": [{
                "name": "Alice",
                "type": "Person",
                "attributes": {
                    "skills": ["python", "neo4j"],
                    "meta": {"level": "Senior", "tags": ["lead"]},
                },
            }],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice is skilled in Python and Neo4j.")

        # Chunk 2: new contact field and updated skills
        self.mock_ner.extract.return_value = {
            "entities": [{
                "name": "Alice",
                "type": "Person",
                "attributes": {
                    "skills": ["python", "neo4j", "ai"],
                    "contact": {"email": "alice@example.com"},
                },
            }],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Contact Alice at alice@example.com.")

        key = ("test-graph", "alice")
        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        self.assertEqual(attrs["meta"], {"level": "Senior", "tags": ["lead"]})
        self.assertEqual(attrs["contact"], {"email": "alice@example.com"})
        self.assertEqual(attrs["skills"], ["python", "neo4j", "ai"])

    def test_merge_entity_handles_non_dict_new_attributes(self):
        """Verify _merge_entity safely preserves existing attributes when new attrs is non-dict."""
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Bob", "type": "Person", "attributes": {"role": "Designer"}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Bob is a designer.")

        # Chunk 2: attributes is None or invalid type
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Bob", "type": "Person", "attributes": None}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Bob attended standup.")

        key = ("test-graph", "bob")
        attrs = json.loads(self.fake_driver.entities[key]["attributes_json"])
        self.assertEqual(attrs, {"role": "Designer"})

    def test_relation_directionality_not_conflated(self):
        """Verify A -> B and B -> A with identical relation name are distinct relationships."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "Bob", "type": "Person", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "Bob", "type": "ADVISES", "fact": "Alice advises Bob"},
                {"source": "Bob", "target": "Alice", "type": "ADVISES", "fact": "Bob advises Alice on design"},
            ],
        }
        ep1 = self.storage.add_text("test-graph", "Alice advises Bob. Bob advises Alice on design.")

        alice_uuid = self.fake_driver.entities[("test-graph", "alice")]["uuid"]
        bob_uuid = self.fake_driver.entities[("test-graph", "bob")]["uuid"]

        rel_ab_key = (alice_uuid, bob_uuid, "test-graph", "ADVISES")
        rel_ba_key = (bob_uuid, alice_uuid, "test-graph", "ADVISES")

        self.assertIn(rel_ab_key, self.fake_driver.relations)
        self.assertIn(rel_ba_key, self.fake_driver.relations)
        self.assertNotEqual(
            self.fake_driver.relations[rel_ab_key]["uuid"],
            self.fake_driver.relations[rel_ba_key]["uuid"],
        )
        self.assertEqual(self.fake_driver.relations[rel_ab_key]["fact"], "Alice advises Bob")
        self.assertEqual(self.fake_driver.relations[rel_ba_key]["fact"], "Bob advises Alice on design")

        # Chunk 2: Repeat only Alice -> Bob; Bob -> Alice should not get new episode_id
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "Bob", "type": "Person", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "Bob", "type": "ADVISES", "fact": "Alice continues to advise Bob"},
            ],
        }
        ep2 = self.storage.add_text("test-graph", "Alice still advises Bob.")

        self.assertEqual(self.fake_driver.relations[rel_ab_key]["episode_ids"], [ep1, ep2])
        self.assertEqual(self.fake_driver.relations[rel_ba_key]["episode_ids"], [ep1])

    def test_relation_different_entity_pairs_same_type_not_conflated(self):
        """Verify same relation name across different entity pairs creates distinct edges."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "Bob", "type": "Person", "attributes": {}},
                {"name": "CompanyX", "type": "Company", "attributes": {}},
                {"name": "CompanyY", "type": "Company", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "CompanyX", "type": "WORKS_AT", "fact": "Alice works at CompanyX"},
                {"source": "Bob", "target": "CompanyX", "type": "WORKS_AT", "fact": "Bob works at CompanyX"},
                {"source": "Alice", "target": "CompanyY", "type": "WORKS_AT", "fact": "Alice consults at CompanyY"},
            ],
        }
        ep = self.storage.add_text("test-graph", "Alice and Bob at CompanyX, Alice consults CompanyY.")

        alice_uuid = self.fake_driver.entities[("test-graph", "alice")]["uuid"]
        bob_uuid = self.fake_driver.entities[("test-graph", "bob")]["uuid"]
        x_uuid = self.fake_driver.entities[("test-graph", "companyx")]["uuid"]
        y_uuid = self.fake_driver.entities[("test-graph", "companyy")]["uuid"]

        self.assertIn((alice_uuid, x_uuid, "test-graph", "WORKS_AT"), self.fake_driver.relations)
        self.assertIn((bob_uuid, x_uuid, "test-graph", "WORKS_AT"), self.fake_driver.relations)
        self.assertIn((alice_uuid, y_uuid, "test-graph", "WORKS_AT"), self.fake_driver.relations)
        self.assertEqual(len(self.fake_driver.relations), 3)

if __name__ == "__main__":
    unittest.main()
