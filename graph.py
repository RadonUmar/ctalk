"""Neo4j projection layer for the ctalk organization knowledge graph.

SQL is the system of record (ADR 0002); this module projects org structure into
Neo4j and serves graph reads + owner discovery. If NEO4J_URI is unset (or the
driver is missing), the graph layer is disabled: writes become no-ops and reads
return empty results, so the core messaging relay keeps working without Neo4j.

Node model (ADR 0001):
    (:Person  {org_id, user_id, name, role, level})
    (:Project {org_id, project_id, name, description, repo_url, context, aliases})
    (:Skill   {org_id, name})
Edges:
    (:Person)-[:REPORTS_TO]->(:Person)        matrix; multiple allowed (ADR 0005)
    (:Person)-[:OWNS {role}]->(:Project)      role: creator|maintainer|contributor
    (:Person)-[:HAS_SKILL]->(:Skill)
    (:Person)-[:INTERACTED_WITH {count}]->(:Person)   projected from user_interactions
"""
import os
import re
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError:  # driver optional; relay still runs without the graph
    GraphDatabase = None

NEO4J_URI = os.environ.get("NEO4J_URI", "")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

_driver = None


def graph_enabled() -> bool:
    return bool(NEO4J_URI and GraphDatabase is not None)


def get_driver():
    global _driver
    if not graph_enabled():
        raise RuntimeError("Neo4j is not configured. Set NEO4J_URI / NEO4J_PASSWORD.")
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def init_graph() -> None:
    """Create the full-text index used for owner discovery (ADR 0007).

    We rely on MERGE for node identity rather than uniqueness constraints, since
    composite/node-key constraints are an Enterprise feature. Best-effort: a failed
    index creation must not crash startup.
    """
    if not graph_enabled():
        return
    stmt = (
        "CREATE FULLTEXT INDEX projectSearch IF NOT EXISTS "
        "FOR (p:Project) ON EACH [p.name, p.description, p.context, p.aliases]"
    )
    with get_driver().session() as session:
        session.run(stmt)


# --- Projection from SQL (ADR 0002) ---------------------------------------

def rebuild_org_graph(org_id: str, snapshot: dict[str, Any]) -> None:
    """Wipe and rebuild one org's subgraph from a SQL snapshot.

    snapshot keys: users, projects, ownership, reporting, skills, interactions.
    Idempotent: safe to call after any change to org structure.
    """
    if not graph_enabled():
        return
    with get_driver().session() as session:
        session.run("MATCH (n {org_id: $org_id}) DETACH DELETE n", org_id=org_id)

        for u in snapshot.get("users", []):
            session.run(
                """
                MERGE (p:Person {org_id: $org_id, user_id: $user_id})
                SET p.name = $name, p.role = $role, p.level = $level
                """,
                org_id=org_id, user_id=u["user_id"], name=u.get("name", ""),
                role=u.get("role", ""), level=u.get("level", ""),
            )

        for pr in snapshot.get("projects", []):
            session.run(
                """
                MERGE (p:Project {org_id: $org_id, project_id: $project_id})
                SET p.name = $name, p.description = $description,
                    p.repo_url = $repo_url, p.context = $context, p.aliases = $aliases
                """,
                org_id=org_id, project_id=pr["project_id"], name=pr.get("name", ""),
                description=pr.get("description", ""), repo_url=pr.get("repo_url", ""),
                context=pr.get("context", ""), aliases=pr.get("aliases", []),
            )

        for o in snapshot.get("ownership", []):
            session.run(
                """
                MATCH (person:Person {org_id: $org_id, user_id: $user_id})
                MATCH (proj:Project {org_id: $org_id, project_id: $project_id})
                MERGE (person)-[r:OWNS]->(proj)
                SET r.role = $role
                """,
                org_id=org_id, user_id=o["user_id"], project_id=o["project_id"],
                role=o.get("role", "contributor"),
            )

        for c in snapshot.get("contributions", []):
            # Attach commit count to the OWNS edge if one exists, else add CONTRIBUTED_TO.
            session.run(
                """
                MATCH (person:Person {org_id: $org_id, user_id: $user_id})
                MATCH (proj:Project {org_id: $org_id, project_id: $project_id})
                OPTIONAL MATCH (person)-[owns:OWNS]->(proj)
                FOREACH (_ IN CASE WHEN owns IS NOT NULL THEN [1] ELSE [] END |
                    SET owns.commits = $commits)
                FOREACH (_ IN CASE WHEN owns IS NULL THEN [1] ELSE [] END |
                    MERGE (person)-[contrib:CONTRIBUTED_TO]->(proj)
                    SET contrib.commits = $commits)
                """,
                org_id=org_id, user_id=c["user_id"], project_id=c["project_id"],
                commits=c.get("commits", 0),
            )

        for r in snapshot.get("reporting", []):
            session.run(
                """
                MATCH (a:Person {org_id: $org_id, user_id: $user_id})
                MATCH (b:Person {org_id: $org_id, user_id: $manager_id})
                MERGE (a)-[:REPORTS_TO]->(b)
                """,
                org_id=org_id, user_id=r["user_id"], manager_id=r["manager_id"],
            )

        for s in snapshot.get("skills", []):
            session.run(
                """
                MERGE (sk:Skill {org_id: $org_id, name: $name})
                WITH sk
                MATCH (p:Person {org_id: $org_id, user_id: $user_id})
                MERGE (p)-[:HAS_SKILL]->(sk)
                """,
                org_id=org_id, name=s["skill"], user_id=s["user_id"],
            )

        for i in snapshot.get("interactions", []):
            if i["user_a_id"] == i["user_b_id"]:
                continue
            session.run(
                """
                MATCH (a:Person {org_id: $org_id, user_id: $a})
                MATCH (b:Person {org_id: $org_id, user_id: $b})
                MERGE (a)-[r:INTERACTED_WITH]->(b)
                SET r.count = $count
                """,
                org_id=org_id, a=i["user_a_id"], b=i["user_b_id"],
                count=i.get("message_count", 1),
            )


# --- Reads for visualization ----------------------------------------------

def fetch_org_graph(org_id: str) -> dict[str, list]:
    """Return {nodes, edges} for the whole org, shaped for the Cytoscape page."""
    if not graph_enabled():
        return {"nodes": [], "edges": []}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    with get_driver().session() as session:
        for rec in session.run("MATCH (p:Person {org_id: $org_id}) RETURN p", org_id=org_id):
            p = rec["p"]
            nodes.append({
                "id": f"person:{p['user_id']}",
                "label": p.get("name") or p["user_id"],
                "type": "Person",
                "role": p.get("role", ""),
                "level": p.get("level", ""),
            })
        for rec in session.run("MATCH (p:Project {org_id: $org_id}) RETURN p", org_id=org_id):
            p = rec["p"]
            nodes.append({
                "id": f"project:{p['project_id']}",
                "label": p.get("name") or p["project_id"],
                "type": "Project",
                "description": p.get("description", ""),
            })
        for rec in session.run("MATCH (s:Skill {org_id: $org_id}) RETURN s", org_id=org_id):
            s = rec["s"]
            nodes.append({"id": f"skill:{s['name']}", "label": s["name"], "type": "Skill"})

        for rec in session.run(
            "MATCH (a:Person {org_id: $org_id})-[:REPORTS_TO]->(b:Person) "
            "RETURN a.user_id AS a, b.user_id AS b", org_id=org_id,
        ):
            edges.append({"source": f"person:{rec['a']}", "target": f"person:{rec['b']}", "type": "REPORTS_TO"})
        for rec in session.run(
            "MATCH (a:Person {org_id: $org_id})-[r:OWNS]->(b:Project) "
            "RETURN a.user_id AS a, b.project_id AS b, r.role AS role, r.commits AS commits", org_id=org_id,
        ):
            edges.append({
                "source": f"person:{rec['a']}", "target": f"project:{rec['b']}",
                "type": "OWNS", "role": rec["role"] or "contributor", "commits": rec["commits"] or 0,
            })
        for rec in session.run(
            "MATCH (a:Person {org_id: $org_id})-[r:CONTRIBUTED_TO]->(b:Project) "
            "RETURN a.user_id AS a, b.project_id AS b, r.commits AS commits", org_id=org_id,
        ):
            edges.append({
                "source": f"person:{rec['a']}", "target": f"project:{rec['b']}",
                "type": "CONTRIBUTED_TO", "commits": rec["commits"] or 0,
            })
        for rec in session.run(
            "MATCH (a:Person {org_id: $org_id})-[:HAS_SKILL]->(b:Skill) "
            "RETURN a.user_id AS a, b.name AS b", org_id=org_id,
        ):
            edges.append({"source": f"person:{rec['a']}", "target": f"skill:{rec['b']}", "type": "HAS_SKILL"})
        for rec in session.run(
            "MATCH (a:Person {org_id: $org_id})-[r:INTERACTED_WITH]->(b:Person) "
            "RETURN a.user_id AS a, b.user_id AS b, r.count AS count", org_id=org_id,
        ):
            edges.append({
                "source": f"person:{rec['a']}", "target": f"person:{rec['b']}",
                "type": "INTERACTED_WITH", "count": rec["count"],
            })
    return {"nodes": nodes, "edges": edges}


# --- Owner discovery (ADR 0007: full-text now, vector later) ---------------

_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _lucene_query(query: str) -> str:
    """Turn free text into a fuzzy OR query so near-miss package names still match."""
    cleaned = _LUCENE_SPECIAL.sub(" ", query)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return query.strip() or "*"
    return " OR ".join(f"{t}~1" for t in tokens)


def find_owner(org_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Find projects matching `query` (by name/aliases/description/context) and their owners."""
    if not graph_enabled():
        return []
    cypher = """
    CALL db.index.fulltext.queryNodes('projectSearch', $q) YIELD node, score
    WHERE node.org_id = $org_id
    OPTIONAL MATCH (person:Person)-[o:OWNS]->(node)
    WITH node, score, collect({user_id: person.user_id, name: person.name, role: o.role}) AS owners
    RETURN node.project_id AS project_id, node.name AS name,
           node.description AS description, node.context AS context,
           score, owners
    ORDER BY score DESC LIMIT $limit
    """
    results: list[dict[str, Any]] = []
    with get_driver().session() as session:
        for rec in session.run(cypher, q=_lucene_query(query), org_id=org_id, limit=limit):
            owners = [o for o in rec["owners"] if o.get("user_id")]
            results.append({
                "project_id": rec["project_id"],
                "name": rec["name"],
                "description": rec["description"],
                "context": rec["context"],
                "score": rec["score"],
                "owners": owners,
            })
    return results


def reporting_distance(org_id: str, user_a: str, user_b: str, max_hops: int = 6) -> int | None:
    """Shortest reporting-graph distance between two people (undirected over REPORTS_TO).

    Returns 0 for the same person, the hop count if connected within max_hops, else None.
    Used by graph-powered routing to prefer organizationally-closer experts.
    """
    if user_a == user_b:
        return 0
    if not graph_enabled():
        return None
    cypher = (
        "MATCH (a:Person {org_id: $org_id, user_id: $a}) "
        "MATCH (b:Person {org_id: $org_id, user_id: $b}) "
        f"MATCH p = shortestPath((a)-[:REPORTS_TO*..{int(max_hops)}]-(b)) "
        "RETURN length(p) AS dist"
    )
    with get_driver().session() as session:
        rec = session.run(cypher, org_id=org_id, a=user_a, b=user_b).single()
        return rec["dist"] if rec else None
