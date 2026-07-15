"""Discover the per-session artifacts Claude Code writes beside a transcript.

Next to ``~/.claude/projects/<slug>/<session-uuid>.jsonl`` Claude Code drops a
sibling directory ``<session-uuid>/`` holding everything the main transcript
*doesn't* keep:

    <session-uuid>/
      subagents/
        agent-<id>.jsonl          # a spawned subagent's FULL transcript
        agent-<id>.meta.json      # {agentType, description, toolUseId}
        workflows/
          wf_<id>/
            agent-<id>.jsonl      # a workflow-orchestrated subagent
            agent-<id>.meta.json
            journal.jsonl         # the workflow's run journal
      workflows/
        scripts/
          <name>-wf_<id>.js       # the workflow orchestration script
      session-memory/
        summary.md                # compaction / handoff digest
      tool-results/
        <id>.txt                  # externalised large tool output

The subagent and workflow ``agent-*.jsonl`` files use the *same* record shape as
a top-level session, so they render through the existing parser/renderer with no
new parsing — this module only handles *discovery* and the small JSON/JS sidecars
(``.meta.json`` and the workflow script ``meta`` block). The main transcript only
ever keeps a subagent's final ``<task-notification>`` result, so exporting these
is what completes the record of a session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# A subagent transcript file: agent-<hex>.jsonl, with a sibling agent-<hex>.meta.json.
_AGENT_PREFIX = "agent-"
# Pull a wf_<id> token out of a workflow script filename / directory name.
_WORKFLOW_ID_RE = re.compile(r"(wf_[0-9a-z]+(?:-[0-9a-z]+)*)", re.IGNORECASE)
# First name:/description: string literal in a workflow script's `meta` block.
_SCRIPT_NAME_RE = re.compile(r"\bname:\s*(['\"])(.*?)\1")
_SCRIPT_DESC_RE = re.compile(r"\bdescription:\s*(['\"])(.*?)\1")


@dataclass(slots=True)
class SubagentRef:
    """A spawned subagent's transcript plus its ``.meta.json`` sidecar fields."""

    path: Path  # agent-<id>.jsonl
    agent_id: str
    agent_type: str | None  # e.g. "general-purpose", "Explore"
    description: str | None  # the task it was given
    tool_use_id: str | None  # links back to the spawning Task call in the parent
    workflow_id: str | None = None  # set when the agent ran inside a workflow


@dataclass(slots=True)
class WorkflowScript:
    """A workflow orchestration script (``.js``) and its self-described ``meta``."""

    path: Path
    workflow_id: str | None
    name: str | None
    description: str | None


@dataclass(slots=True)
class WorkflowRun:
    """One workflow execution: its journal plus the agents it orchestrated."""

    workflow_id: str
    run_dir: Path
    journal_path: Path | None
    agents: list[SubagentRef]


@dataclass(slots=True)
class SessionArtifacts:
    """Everything Claude Code stored beside one session transcript."""

    session_id: str
    artifact_dir: Path
    summaries: list[Path]
    subagents: list[SubagentRef]  # direct (non-workflow) subagents
    workflow_runs: list[WorkflowRun]
    scripts: list[WorkflowScript]
    tool_results: list[Path]

    def __bool__(self) -> bool:
        return bool(
            self.summaries
            or self.subagents
            or self.workflow_runs
            or self.scripts
            or self.tool_results
        )


@dataclass(slots=True)
class ArtifactSelection:
    """Which artifact categories an export should include (all default off)."""

    memories: bool = False
    subagents: bool = False
    summaries: bool = False
    workflows: bool = False  # workflow scripts + run journals + orchestrated agents
    tool_results: bool = False

    @classmethod
    def resolve(
        cls,
        *,
        memories: bool = False,
        subagents: bool = False,
        summaries: bool = False,
        workflows: bool = False,
        tool_results: bool = False,
        full: bool = False,
    ) -> ArtifactSelection:
        """Build a selection from CLI flags; ``full`` turns everything on."""
        if full:
            return cls(True, True, True, True, True)
        return cls(memories, subagents, summaries, workflows, tool_results)

    @property
    def any_session_scoped(self) -> bool:
        """True when at least one *per-session* category is selected.

        ``memories`` is project-scoped (written once per project, not per session),
        so it is excluded here — it drives a separate export step.
        """
        return self.subagents or self.summaries or self.workflows or self.tool_results

    def __bool__(self) -> bool:
        return any(
            (self.memories, self.subagents, self.summaries, self.workflows, self.tool_results)
        )


def session_artifact_dir(project_dir: Path, session_id: str) -> Path:
    """The ``<session-uuid>/`` sibling directory of a session's ``.jsonl``."""
    return project_dir / session_id


def _agent_id_from(path: Path) -> str:
    stem = path.stem  # agent-<id>
    return stem[len(_AGENT_PREFIX) :] if stem.startswith(_AGENT_PREFIX) else stem


def _read_agent_meta(jsonl_path: Path) -> dict[str, Any]:
    """Read the ``agent-<id>.meta.json`` sitting next to a subagent transcript."""
    meta_path = jsonl_path.with_suffix(".meta.json")
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _make_subagent_ref(jsonl_path: Path, *, workflow_id: str | None = None) -> SubagentRef:
    meta = _read_agent_meta(jsonl_path)
    return SubagentRef(
        path=jsonl_path,
        agent_id=_agent_id_from(jsonl_path),
        agent_type=meta.get("agentType"),
        description=meta.get("description"),
        tool_use_id=meta.get("toolUseId"),
        workflow_id=workflow_id,
    )


def _workflow_id_from(name: str) -> str | None:
    m = _WORKFLOW_ID_RE.search(name)
    return m.group(1) if m else None


def _script_meta(path: Path) -> tuple[str | None, str | None]:
    """Extract ``name`` / ``description`` from a workflow script's ``meta`` literal.

    The script is JavaScript; we never execute it. The ``meta`` block is the first
    object in the file and its ``name``/``description`` are plain string literals,
    so a bounded regex over the head of the file is enough for a label.
    """
    try:
        head = path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return None, None
    name = _SCRIPT_NAME_RE.search(head)
    desc = _SCRIPT_DESC_RE.search(head)
    return (name.group(2) if name else None, desc.group(2) if desc else None)


def _discover_subagents(subagents_dir: Path) -> list[SubagentRef]:
    """Direct subagents only — those that did not run inside a workflow."""
    if not subagents_dir.is_dir():
        return []
    return [_make_subagent_ref(p) for p in sorted(subagents_dir.glob(f"{_AGENT_PREFIX}*.jsonl"))]


def _discover_workflow_runs(subagents_dir: Path) -> list[WorkflowRun]:
    """Workflow executions live under ``subagents/workflows/wf_<id>/``."""
    workflows_dir = subagents_dir / "workflows"
    if not workflows_dir.is_dir():
        return []
    runs: list[WorkflowRun] = []
    for run_dir in sorted(d for d in workflows_dir.iterdir() if d.is_dir()):
        workflow_id = run_dir.name
        journal = run_dir / "journal.jsonl"
        agents = [
            _make_subagent_ref(p, workflow_id=workflow_id)
            for p in sorted(run_dir.glob(f"{_AGENT_PREFIX}*.jsonl"))
        ]
        runs.append(
            WorkflowRun(
                workflow_id=workflow_id,
                run_dir=run_dir,
                journal_path=journal if journal.is_file() else None,
                agents=agents,
            )
        )
    return runs


def _discover_scripts(artifact_dir: Path) -> list[WorkflowScript]:
    """Workflow orchestration scripts live under ``workflows/scripts/``."""
    scripts_dir = artifact_dir / "workflows" / "scripts"
    if not scripts_dir.is_dir():
        return []
    scripts: list[WorkflowScript] = []
    for p in sorted(scripts_dir.glob("*.js")):
        name, desc = _script_meta(p)
        scripts.append(
            WorkflowScript(
                path=p,
                workflow_id=_workflow_id_from(p.name),
                name=name,
                description=desc,
            )
        )
    return scripts


def discover_session_artifacts(project_dir: Path, session_id: str) -> SessionArtifacts:
    """Find every artifact Claude Code stored for one session.

    Cheap: globs a handful of well-known subdirectories and reads only the tiny
    ``.meta.json`` / script-head sidecars — never the (potentially large) agent
    transcripts, which are loaded lazily at render time.
    """
    artifact_dir = session_artifact_dir(project_dir, session_id)
    if not artifact_dir.is_dir():
        return SessionArtifacts(session_id, artifact_dir, [], [], [], [], [])

    subagents_dir = artifact_dir / "subagents"
    summary_dir = artifact_dir / "session-memory"
    tool_results_dir = artifact_dir / "tool-results"

    summaries = sorted(summary_dir.glob("*.md")) if summary_dir.is_dir() else []
    tool_results = (
        sorted(p for p in tool_results_dir.iterdir() if p.is_file())
        if tool_results_dir.is_dir()
        else []
    )

    return SessionArtifacts(
        session_id=session_id,
        artifact_dir=artifact_dir,
        summaries=summaries,
        subagents=_discover_subagents(subagents_dir),
        workflow_runs=_discover_workflow_runs(subagents_dir),
        scripts=_discover_scripts(artifact_dir),
        tool_results=tool_results,
    )


def summarize_journal(journal_path: Path) -> dict[str, Any]:
    """Reduce a workflow ``journal.jsonl`` to counts of started/completed agents.

    The journal is an append-only run log of ``{type, key, agentId}`` records
    (``type`` ∈ ``started`` / ``result``). We surface a compact rollup rather than
    the raw log, which is only meaningful to the workflow resume machinery.
    """
    started = completed = 0
    agent_ids: set[str] = set()
    with journal_path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "started":
                started += 1
            elif kind == "result":
                completed += 1
            agent_id = obj.get("agentId")
            if agent_id:
                agent_ids.add(str(agent_id))
    return {
        "workflow_id": _workflow_id_from(journal_path.parent.name) or journal_path.parent.name,
        "agents_started": started,
        "agents_completed": completed,
        "distinct_agents": len(agent_ids),
    }
