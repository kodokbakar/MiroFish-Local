#!/usr/bin/env python3
"""
Neo4j Schema Migration & Verification Script
Applies all constraints and indexes, then monitors index population until ONLINE.

Usage:
    python backend/scripts/migrate_neo4j_schema.py [--timeout SECONDS] [--check-only]
"""

import os
import sys
import time
import argparse
import logging
from typing import List, Dict, Any

# Ensure backend root is in sys.path
backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_root not in sys.path:
    sys.path.insert(0, backend_root)

from neo4j import GraphDatabase
from app.config import Config
from app.storage import neo4j_schema

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("graph-migration")


def apply_schema(driver) -> None:
    """Execute all schema constraints and index creation queries."""
    logger.info("Applying Neo4j schema queries (constraints + indexes)...")
    with driver.session() as session:
        for idx, query in enumerate(neo4j_schema.ALL_SCHEMA_QUERIES, 1):
            query_clean = query.strip()
            first_line = query_clean.split("\n")[0]
            try:
                session.run(query)
                logger.info(f"[{idx}/{len(neo4j_schema.ALL_SCHEMA_QUERIES)}] Applied: {first_line}")
            except Exception as e:
                logger.warning(f"[{idx}/{len(neo4j_schema.ALL_SCHEMA_QUERIES)}] Warning: {first_line} -> {e}")


def get_index_status(driver) -> List[Dict[str, Any]]:
    """Query Neo4j for all index states and population percentages."""
    with driver.session() as session:
        try:
            res = session.run("SHOW INDEXES YIELD name, state, populationPercent, type")
            return [dict(rec) for rec in res]
        except Exception as e:
            logger.warning(f"Failed to query SHOW INDEXES: {e}")
            return []


def wait_for_indexes(driver, timeout_sec: float = 60.0, poll_interval: float = 1.0) -> bool:
    """Monitor indexes until all are ONLINE or timeout is reached."""
    logger.info(f"Monitoring index population states (timeout: {timeout_sec}s)...")
    start = time.time()

    while time.time() - start < timeout_sec:
        indexes = get_index_status(driver)
        if not indexes:
            logger.warning("No index records returned; continuing.")
            return True

        populating = [idx for idx in indexes if idx.get("state") == "POPULATING"]
        failed = [idx for idx in indexes if idx.get("state") == "FAILED"]

        if failed:
            logger.error(f"One or more indexes entered FAILED state: {[f['name'] for f in failed]}")
            return False

        if not populating:
            logger.info("All indexes are ONLINE!")
            _print_status_table(indexes)
            return True

        pop_descs = [f"{p.get('name', 'unnamed')} ({p.get('populationPercent', 0):.1f}%)" for p in populating]
        logger.info(f"Waiting on {len(populating)} populating index(es)... ({pop_descs})")
        time.sleep(poll_interval)

    logger.error(f"Timed out after {timeout_sec}s waiting for indexes to become ONLINE.")
    _print_status_table(get_index_status(driver))
    return False


def _print_status_table(indexes: List[Dict[str, Any]]) -> None:
    """Print formatted summary table of indexes."""
    print("\n" + "=" * 70)
    print(f"{'Index Name':<35} {'Type':<12} {'State':<12} {'Population':<10}")
    print("-" * 70)
    for idx in sorted(indexes, key=lambda x: x.get("name", "")):
        name = idx.get("name", "unnamed")
        itype = str(idx.get("type", "N/A"))
        state = str(idx.get("state", "UNKNOWN"))
        pct = f"{idx.get('populationPercent', 100.0):.1f}%"
        print(f"{name:<35} {itype:<12} {state:<12} {pct:<10}")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="MiroFish Neo4j Schema Migration & Index Verifier")
    parser.add_argument("--timeout", type=float, default=60.0, help="Max seconds to wait for indexes to be ONLINE (default: 60)")
    parser.add_argument("--check-only", action="store_true", help="Only inspect current index statuses without applying schema")
    args = parser.parse_args()

    logger.info(f"Connecting to Neo4j at {Config.NEO4J_URI} as user '{Config.NEO4J_USER}'...")
    try:
        driver = GraphDatabase.driver(
            Config.NEO4J_URI,
            auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
        )
        driver.verify_connectivity()
        logger.info("Neo4j connection successful.")
    except Exception as e:
        logger.error(f"Failed to connect to Neo4j: {e}")
        sys.exit(1)

    try:
        if not args.check_only:
            apply_schema(driver)

        success = wait_for_indexes(driver, timeout_sec=args.timeout)
        if not success:
            sys.exit(1)
        logger.info("Schema migration and index verification completed successfully.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
