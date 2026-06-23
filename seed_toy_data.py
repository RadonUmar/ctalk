"""Seed a toy organization ("toyco") into SQL + Neo4j for graph development/demo.

Run from the repo root with Neo4j reachable:
    NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=ctalkdev123 python3 seed_toy_data.py

Idempotent: wipes toyco first. Prints an auth token for the /graph page.
"""
import os

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "ctalkdev123")

import relay  # noqa: E402  (env must be set before import)
import graph  # noqa: E402

ORG = "toyco"
ADMIN_PASSWORD = "toyco-admin-password"
USER_PASSWORD = "toyco-user-pass"

# (user_id, name, role, level, reports_to[], skills[])
PEOPLE = [
    ("dana",  "Dana Reed",     "CTO",                "exec",    [],               ["leadership", "architecture", "terraform"]),
    ("mia",   "Mia Chen",      "VP Engineering",     "vp",      ["dana"],         ["leadership", "python", "planning"]),
    ("omar",  "Omar Haddad",   "Eng Manager, Platform", "manager", ["mia"],      ["python", "infrastructure", "postgres", "fastapi"]),
    ("priya", "Priya Nair",    "Eng Manager, Product",  "manager", ["mia"],      ["python", "product", "billing"]),
    ("leo",   "Leo Martins",   "Senior Engineer",    "senior",  ["omar", "priya"],  ["python", "react", "typescript", "auth"]),
    ("sara",  "Sara Park",     "Engineer",           "mid",     ["omar"],         ["python", "fastapi", "auth", "postgres"]),
    ("kenji", "Kenji Ito",     "Engineer",           "mid",     ["priya"],        ["python", "etl", "billing", "sql"]),
    ("nina",  "Nina Alvarez",  "Data Scientist",     "senior",  ["priya", "mia"], ["python", "etl", "analytics", "ml"]),
]

# project_id -> dict
PROJECTS = {
    "authsvc": {
        "name": "Auth Service",
        "aliases": ["auth-service", "authentication", "login-service"],
        "description": "Organization login, password hashing, and auth tokens.",
        "context": "Handles org login, PBKDF2 password hashing, and bearer auth tokens. "
                   "Tokens are SHA-256 hashed at rest. Token rotation is a known TODO.",
        "owners": [("sara", "creator"), ("leo", "maintainer")],
    },
    "billing": {
        "name": "Billing Pipeline",
        "aliases": ["billing-pipeline", "payments", "invoicing"],
        "description": "Batch billing and invoice generation.",
        "context": "Nightly batch billing + invoice generation, integrates Stripe, retries "
                   "failed charges with exponential backoff (three attempts).",
        "owners": [("kenji", "creator"), ("priya", "maintainer")],
    },
    "relay-core": {
        "name": "Relay Core",
        "aliases": ["relay", "mcp-relay", "ctalk-relay"],
        "description": "The FastAPI relay and MCP endpoint.",
        "context": "FastAPI app serving REST + the remote MCP endpoint at /mcp. Dual "
                   "SQLite/Postgres storage selected by DATABASE_URL.",
        "owners": [("omar", "creator"), ("sara", "maintainer")],
    },
    "datapipe": {
        "name": "Data Pipeline",
        "aliases": ["data-pipeline", "etl", "analytics"],
        "description": "Nightly ETL into the warehouse.",
        "context": "Airflow DAGs run nightly ETL into the warehouse and own the reporting "
                   "tables consumed by analytics dashboards.",
        "owners": [("nina", "creator"), ("kenji", "maintainer")],
    },
    "webapp": {
        "name": "Web App",
        "aliases": ["web-app", "frontend", "dashboard"],
        "description": "React dashboard and org graph viewer.",
        "context": "React dashboard talking to relay-core; hosts the Cytoscape org-graph "
                   "visualization page.",
        "owners": [("leo", "creator"), ("kenji", "contributor")],
    },
    "infra": {
        "name": "Infra & Deploy",
        "aliases": ["infrastructure", "deploy", "terraform"],
        "description": "Terraform and deploy configuration.",
        "context": "Terraform plus Railway/Render deploy configs; manages the DATABASE_URL "
                   "and other secrets across environments.",
        "owners": [("omar", "creator"), ("dana", "maintainer")],
    },
}

# pairs that have talked, with weight
INTERACTIONS = [("leo", "sara", 3), ("kenji", "nina", 2), ("omar", "mia", 1), ("leo", "priya", 2)]

OPTED_OUT = {"dana"}  # demonstrates the commit-history consent gate (no commits stored)

# (project_id, user_id, commits) -- a contributor who is NOT in a project's owners list
# becomes a CONTRIBUTED_TO edge (e.g. leo on relay-core, sara on webapp).
CONTRIBUTIONS = [
    ("authsvc", "sara", 120), ("authsvc", "leo", 38),
    ("billing", "kenji", 95), ("billing", "priya", 22),
    ("relay-core", "omar", 140), ("relay-core", "sara", 60), ("relay-core", "leo", 15),
    ("datapipe", "nina", 110), ("datapipe", "kenji", 30),
    ("webapp", "leo", 80), ("webapp", "kenji", 25), ("webapp", "sara", 20),
    ("infra", "omar", 55),
]


def wipe() -> None:
    with relay.db() as conn:
        for table in ("project_ownership", "project_context", "projects", "reporting",
                      "skills", "contributions", "auth_tokens", "org_messages",
                      "user_interactions", "users"):
            relay.execute(conn, f"DELETE FROM {table} WHERE org_id = ?", (ORG,))
        relay.execute(conn, "DELETE FROM organizations WHERE id = ?", (ORG,))


def seed() -> str:
    relay.init_db()
    graph.init_graph()
    wipe()
    now = relay.utc_now()

    with relay.db() as conn:
        relay.execute(conn,
            "INSERT INTO organizations (id, name, admin_password_hash, created_at) VALUES (?, ?, ?, ?)",
            (ORG, "Toy Co", relay.hash_secret(ADMIN_PASSWORD), now))

        for user_id, name, role, level, _reports, _skills in PEOPLE:
            relay.execute(conn,
                """
                INSERT INTO users (org_id, user_id, name, password_hash, ownership, notes,
                                   profile_notes, created_at, role, level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ORG, user_id, name, relay.hash_secret(USER_PASSWORD), relay.json_list([]),
                 "", "", now, role, level))

        for user_id, _name, _role, _level, reports, skills in PEOPLE:
            relay._set_reporting(conn, ORG, user_id, reports)
            relay._set_skills(conn, ORG, user_id, skills)

        for project_id, p in PROJECTS.items():
            relay._upsert_project(conn, ORG, project_id, p["name"], p["description"],
                                  p.get("repo_url", ""), p["aliases"], p["context"])
            for owner_id, owner_role in p["owners"]:
                relay._set_ownership(conn, ORG, project_id, owner_id, owner_role)

        # commit-history consent: everyone opts in except OPTED_OUT
        for user_id, *_rest in PEOPLE:
            relay.execute(conn, "UPDATE users SET commit_history_opt_in = ? WHERE org_id = ? AND user_id = ?",
                          (0 if user_id in OPTED_OUT else 1, ORG, user_id))
        # contributions are only stored for opted-in users
        for project_id, user_id, commits in CONTRIBUTIONS:
            if user_id in OPTED_OUT:
                continue
            relay.execute(conn,
                "INSERT INTO contributions (org_id, project_id, user_id, commits, last_commit_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(org_id, project_id, user_id) DO UPDATE SET commits = excluded.commits",
                (ORG, project_id, user_id, commits, now))

    for a, b, weight in INTERACTIONS:
        for _ in range(weight):
            relay.record_interaction(ORG, a, b)

    relay.reproject_org(ORG)

    token = relay.make_auth_token()
    with relay.db() as conn:
        relay.execute(conn,
            "INSERT INTO auth_tokens (token_hash, org_id, user_id, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (relay.hash_token(token), ORG, "dana", now, now))
    return token


if __name__ == "__main__":
    auth_token = seed()
    print(f"Seeded org '{ORG}': {len(PEOPLE)} people, {len(PROJECTS)} projects.")
    print(f"graph_enabled={graph.graph_enabled()}")
    print(f"AUTH_TOKEN={auth_token}")
    print(f"Open: http://localhost:8000/graph?token={auth_token}")
