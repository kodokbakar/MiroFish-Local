"""
Neo4j Schema — Cypher queries for index creation and schema management.

Called by Neo4jStorage.create_graph() to set up vector + fulltext indexes.
"""
from ..config import Config


# Constraints
CREATE_GRAPH_UUID_CONSTRAINT = """
CREATE CONSTRAINT graph_uuid IF NOT EXISTS
FOR (g:Graph) REQUIRE g.graph_id IS UNIQUE
"""

CREATE_ENTITY_UUID_CONSTRAINT = """
CREATE CONSTRAINT entity_uuid IF NOT EXISTS
FOR (n:Entity) REQUIRE n.uuid IS UNIQUE
"""

CREATE_EPISODE_UUID_CONSTRAINT = """
CREATE CONSTRAINT episode_uuid IF NOT EXISTS
FOR (ep:Episode) REQUIRE ep.uuid IS UNIQUE
"""

# Property indexes (for fast MERGE lookups and edge filtering)
CREATE_ENTITY_GRAPH_NAME_INDEX = """
CREATE INDEX entity_graph_name IF NOT EXISTS
FOR (n:Entity) ON (n.graph_id, n.name_lower)
"""

CREATE_RELATION_GRAPH_ID_INDEX = """
CREATE INDEX relation_graph_id IF NOT EXISTS
FOR ()-[r:RELATION]-() ON (r.graph_id)
"""

# Vector indexes (Neo4j 5.11+)
CREATE_ENTITY_VECTOR_INDEX = f"""
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
FOR (n:Entity) ON (n.embedding)
OPTIONS {{indexConfig: {{
    `vector.dimensions`: {Config.EMBEDDING_DIMENSIONS},
    `vector.similarity_function`: 'cosine'
}}}}
"""

CREATE_RELATION_VECTOR_INDEX = f"""
CREATE VECTOR INDEX fact_embedding IF NOT EXISTS
FOR ()-[r:RELATION]-() ON (r.fact_embedding)
OPTIONS {{indexConfig: {{
    `vector.dimensions`: {Config.EMBEDDING_DIMENSIONS},
    `vector.similarity_function`: 'cosine'
}}}}
"""

# Fulltext indexes (for BM25 keyword search)
CREATE_ENTITY_FULLTEXT_INDEX = """
CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS
FOR (n:Entity) ON EACH [n.name, n.summary]
"""

CREATE_FACT_FULLTEXT_INDEX = """
CREATE FULLTEXT INDEX fact_fulltext IF NOT EXISTS
FOR ()-[r:RELATION]-() ON EACH [r.fact, r.name]
"""

# Query to inspect index population status
CHECK_INDEXES_QUERY = """
SHOW INDEXES YIELD name, state, populationPercent, type
"""

# All schema queries to run on startup
ALL_SCHEMA_QUERIES = [
    CREATE_GRAPH_UUID_CONSTRAINT,
    CREATE_ENTITY_UUID_CONSTRAINT,
    CREATE_EPISODE_UUID_CONSTRAINT,
    CREATE_ENTITY_GRAPH_NAME_INDEX,
    CREATE_RELATION_GRAPH_ID_INDEX,
    CREATE_ENTITY_VECTOR_INDEX,
    CREATE_RELATION_VECTOR_INDEX,
    CREATE_ENTITY_FULLTEXT_INDEX,
    CREATE_FACT_FULLTEXT_INDEX,
]
