"""Project auto-tagger for email/WhatsApp threads.

Classifies threads into ongoing founder initiatives (fundraise-seed,
hiring-eng, hardware-procurement, investor-relations, etc.) using an LLM.
All operations are best-effort: failures are swallowed and return safe defaults
so this module never disrupts the main pipeline.

Stdlib only.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

try:
    from assistant.storage import operating_state as os_store
except ImportError:
    os_store = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import sqlite3

    from assistant.config import Settings
    from assistant.llm.client import LLMClient


# ---------------------------------------------------------------------------
# 1. Prompt builder
# ---------------------------------------------------------------------------

def _build_tag_prompt(
    subject: str,
    contact_name: str,
    category: str,
    snippet: str,
    existing_projects: list[str],
) -> str:
    """Build the LLM prompt for project classification."""
    projects_str = ", ".join(existing_projects) if existing_projects else "(none yet)"
    snippet_truncated = snippet[:300]
    return (
        "Determine which project this email/message belongs to.\n"
        "Projects are ongoing founder initiatives. Existing projects: "
        f"{projects_str}.\n\n"
        f"Thread subject: {subject}\n"
        f"Contact: {contact_name}\n"
        f"Category: {category}\n"
        f"Snippet: {snippet_truncated}\n\n"
        'Return JSON only: {"project": "<kebab-case-name>", "confidence": 0.0}\n'
        "Rules:\n"
        "- Reuse an existing project name if it fits (exact match preferred).\n"
        "- Only create a new project name if clearly a new initiative.\n"
        "- Use null if this is admin/newsletter/spam with no project.\n"
        "- Confidence must be 0.0-1.0. Only tag if confidence >= 0.6."
    )


# ---------------------------------------------------------------------------
# 2. Auto-tag a thread
# ---------------------------------------------------------------------------

def auto_tag_thread(
    thread_id: str,
    subject: str,
    contact_name: str,
    category: str,
    snippet: str,
    db: "sqlite3.Connection",
    settings: "Settings",
    llm_client: "LLMClient",
) -> Optional[str]:
    """Tag a thread with a project name using the LLM.

    Returns the project name (kebab-case) on success, or None if:
    - project tagging is disabled
    - the model is not confident enough
    - the model returns null for the project
    - any error occurs (fail silently)
    """
    try:
        if not getattr(settings, "project_tagging_enabled", True):
            return None

        existing = get_all_project_names(db)
        prompt = _build_tag_prompt(subject, contact_name, category, snippet, existing)

        result_text = llm_client.complete_text(
            system_prefix="You are a project classifier. Output only valid JSON.",
            user_prompt=prompt,
            max_tokens=200,
            use_opus=False,
        )

        # Extract the first {...} JSON object from the response.
        match = re.search(r"\{[^{}]*\}", result_text, re.DOTALL)
        if not match:
            return None

        result = json.loads(match.group())
        project = result.get("project")
        confidence = float(result.get("confidence", 0.0))

        if project is None or confidence < 0.6:
            return None

        project_name = project.lower().strip().replace(" ", "-")

        if os_store is not None:
            try:
                os_store.upsert_project(db, project_name)
            except Exception:  # noqa: BLE001
                pass

        if os_store is not None:
            try:
                os_store.upsert_thread(
                    db,
                    thread_id,
                    channel=None,
                    status="quiet",
                    project_id=project_name,
                )
            except Exception:  # noqa: BLE001
                pass

        return project_name

    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 3. Get all project names
# ---------------------------------------------------------------------------

def get_all_project_names(db: "sqlite3.Connection") -> list[str]:
    """Return all project names from the projects table, sorted alphabetically.

    Returns an empty list if the table does not exist or any error occurs.
    """
    try:
        cursor = db.execute("SELECT name FROM projects ORDER BY name")
        return [row[0] for row in cursor.fetchall()]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 4. Get summary for one project
# ---------------------------------------------------------------------------

def get_project_summary(project_name: str, db: "sqlite3.Connection") -> dict:
    """Return a summary dict for a single project.

    Keys: name, status, thread_count, recent_subjects (list[str]).
    Returns {} on any error.
    """
    try:
        row = db.execute(
            "SELECT name, status FROM projects WHERE name = ?",
            (project_name,),
        ).fetchone()

        if row is None:
            return {}

        name, status = row[0], row[1]

        count_row = db.execute(
            "SELECT COUNT(*) FROM threads WHERE project_id = ?",
            (project_name,),
        ).fetchone()
        thread_count = int(count_row[0]) if count_row else 0

        subject_rows = db.execute(
            "SELECT subject FROM threads WHERE project_id = ? "
            "ORDER BY last_activity_ts DESC LIMIT 5",
            (project_name,),
        ).fetchall()
        recent_subjects = [r[0] for r in subject_rows if r[0]]

        return {
            "name": name,
            "status": status,
            "thread_count": thread_count,
            "recent_subjects": recent_subjects,
        }
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 5. Get all project summaries
# ---------------------------------------------------------------------------

def get_all_project_summaries(db: "sqlite3.Connection") -> list[dict]:
    """Return a summary dict for every project in the projects table.

    Returns [] on any error.
    """
    try:
        rows = db.execute("SELECT name FROM projects ORDER BY name").fetchall()
        summaries = []
        for (name,) in rows:
            summary = get_project_summary(name, db)
            if summary:
                summaries.append(summary)
        return summaries
    except Exception:  # noqa: BLE001
        return []
