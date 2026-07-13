from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .api import serve
from .extraction import ExtractorConfig
from .extraction_evaluation import evaluate_extractor
from .evaluation import (
    EvaluationTask,
    FullHistoryPolicy,
    ResumePolicy,
    SubprocessTaskRunner,
    assert_resume_quality,
    assert_task_quality,
    evaluate_policies,
    evaluate_with_runner,
)
from .hooks import (
    install_git_precommit,
    install_hooks,
    process_hook,
    resolve_hook_project,
)
from .installer import (
    confirm_data_deletion,
    detect_agents,
    run_setup,
    run_uninstall,
    select_interactively,
)
from .mcp import serve_stdio
from .paths import (
    resolve_project_database,
    resolve_runtime_database,
    resolve_runtime_project,
)
from .physical import PhysicalCandidate, PhysicalMemoryGovernor, Placement
from .service import MemoryService
from .witness import WitnessRegistry


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _print(value: Any) -> None:
    print(json.dumps(_plain(value), ensure_ascii=False, indent=2))


def _json_object(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return result


def _json_array(value: str) -> list[str]:
    result = json.loads(value)
    if not isinstance(result, list) or not result or not all(isinstance(item, str) for item in result):
        raise argparse.ArgumentTypeError("expected a non-empty JSON array of strings")
    return result


def _identifier_list(values: list[str]) -> list[str]:
    if len(values) == 1 and values[0].lstrip().startswith("["):
        return _json_array(values[0])
    return values


def _hook_json_input(stream: Any) -> dict[str, Any]:
    """Read native hook JSON as UTF-8 while accepting an optional UTF-8 BOM."""
    raw_stream = getattr(stream, "buffer", stream)
    source = raw_stream.read()
    if isinstance(source, str):
        source = source.removeprefix("\ufeff")
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def _evaluation_tasks(path: str | Path) -> list[EvaluationTask]:
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("evaluation task file must contain a JSON array")
    return [
        EvaluationTask(
            id=item["id"],
            query=item.get("query", item.get("task_input", "")),
            required_evidence=tuple(item.get("required_evidence", ())),
            branch_id=item.get("branch_id", "main"),
            task_input=item.get("task_input", item.get("query", "")),
            expected_output=item.get("expected_output"),
            metadata=item.get("metadata"),
        )
        for item in values
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="joiny-mnemonic")
    parser.add_argument(
        "--db", default=None,
        help="SQLite memory database; defaults to project .joiny-mnemonic with legacy fallback",
    )
    parser.add_argument("--project-root", default=".", help="project root used for snapshot fingerprints")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize the durable store")

    setup = commands.add_parser(
        "setup", help="detect products and configure hooks, MCP, and optional components"
    )
    setup.add_argument(
        "--agent",
        action="append",
        choices=["claude-code", "codex", "opencode", "openhands"],
        default=[],
    )
    setup.add_argument(
        "--plugin",
        action="append",
        choices=["semantic-local", "knowledge-graph", "nuextract-local"],
        default=[],
    )
    setup.add_argument("--all-plugins", action="store_true")
    setup.add_argument("--scope", choices=["project", "global"])
    setup.add_argument("--with-mcp", action="store_true")
    setup.add_argument("--without-hooks", action="store_true")
    setup.add_argument("--skip-plugin-install", action="store_true")
    setup.add_argument(
        "--enable-extraction",
        action="store_true",
        help="explicitly bootstrap/request the experimental automatic-extraction policy",
    )
    setup.add_argument("--source-root")
    setup.add_argument(
        "--yes", action="store_true",
        help="accept detected products non-interactively",
    )
    setup.add_argument("--dry-run", action="store_true")

    uninstall = commands.add_parser(
        "uninstall",
        help="remove Joiny-owned hooks and MCP registrations while preserving memory data",
    )
    uninstall.add_argument(
        "--agent",
        action="append",
        choices=["claude-code", "codex", "opencode", "openhands"],
        default=[],
    )
    uninstall.add_argument("--scope", choices=["project", "global"], default="project")
    uninstall.add_argument("--without-hooks", action="store_true")
    uninstall.add_argument("--without-mcp", action="store_true")
    data_action = uninstall.add_mutually_exclusive_group()
    data_action.add_argument(
        "--delete-data",
        action="store_true",
        help="delete database, sidecars, migration backups and artifacts after cleanup",
    )
    data_action.add_argument(
        "--keep-data",
        action="store_true",
        help="preserve durable project data without an interactive prompt",
    )
    uninstall.add_argument("--dry-run", action="store_true")

    session = commands.add_parser("session-start")
    session.add_argument("--agent", required=True)
    session.add_argument("--branch", default="main")
    session.add_argument("--capabilities", type=_json_object, default={})

    branch = commands.add_parser("branch-create")
    branch.add_argument("id")
    branch.add_argument("--parent", default="main")
    branch.add_argument("--fork-seq", type=int)

    append = commands.add_parser("append")
    append.add_argument("--kind", required=True)
    append.add_argument("--content", required=True)
    append.add_argument("--role")
    append.add_argument("--branch", default="main")
    append.add_argument("--session")
    append.add_argument("--payload", type=_json_object, default={})
    append.add_argument("--file", action="append", default=[])

    artifact = commands.add_parser("artifact")
    artifact.add_argument("path")
    artifact.add_argument("--name")
    artifact.add_argument("--mime", default="application/octet-stream")
    artifact.add_argument("--branch", default="main")
    artifact.add_argument("--session")

    block = commands.add_parser("block-set")
    block.add_argument("name", choices=["instructions", "goal", "constraints", "decisions", "open_tasks"])
    block.add_argument("content")
    block.add_argument("--branch", default="main")
    block.add_argument("--session")
    block.add_argument("--source", action="append", default=[])

    derive = commands.add_parser("derive")
    derive.add_argument(
        "memory_type",
        choices=[
            "fact", "decision", "task", "preference", "failure", "lesson", "summary", "index"
        ],
    )
    derive.add_argument("content")
    derive.add_argument("--summary", default="")
    derive.add_argument("--source", action="append", required=True)
    derive.add_argument("--file", action="append", default=[])
    derive.add_argument("--branch", default="main")
    derive.add_argument("--risk", type=float, default=0.0)
    derive.add_argument("--cost", type=float, default=1.0)
    derive.add_argument("--supersedes")
    derive.add_argument("--valid-from")
    derive.add_argument("--valid-to")
    derive.add_argument("--temporal-expression")

    search = commands.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--branch", default="main")
    search.add_argument("--type", action="append", default=[])
    search.add_argument("--file")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--exact", action="store_true")
    search.add_argument("--no-events", action="store_true")
    search.add_argument("--no-semantic", action="store_true")
    search.add_argument("--staleness", action="store_true")
    search.add_argument("--valid-at")
    search.add_argument("--known-at")
    search.add_argument("--current", action="store_true")
    search.add_argument("--include-unknown-validity", action="store_true")
    search.add_argument("--history", action="store_true")

    stale = commands.add_parser("stale")
    stale.add_argument("--branch", default="main")
    selector = stale.add_mutually_exclusive_group()
    selector.add_argument("--id")
    selector.add_argument("--file")
    stale.add_argument("--threshold", type=int, default=3)

    precheck = commands.add_parser("precheck")
    precheck.add_argument("--branch", default="main")
    precheck.add_argument("--file", action="append", default=[])
    precheck.add_argument("--staged", action="store_true")
    precheck.add_argument("--command", dest="candidate_command")
    precheck.add_argument("--strict", action="store_true")

    commands.add_parser("extraction-status")
    commands.add_parser("extraction-worker", help=argparse.SUPPRESS)

    extraction_process = commands.add_parser("extraction-process")
    extraction_process.add_argument("--limit", type=int)

    extraction_retry = commands.add_parser("extraction-retry")
    extraction_retry.add_argument("--limit", type=int)

    extraction_reprocess = commands.add_parser("extraction-reprocess")
    extraction_reprocess.add_argument("config", type=_json_object)
    extraction_reprocess.add_argument("--limit", type=int)

    extraction_candidates = commands.add_parser("extraction-candidates")
    extraction_candidates.add_argument("--status")

    candidate_request = commands.add_parser("candidate-request")
    candidate_request.add_argument("candidate_id")
    candidate_request.add_argument("action", choices=["confirm", "reject", "supersede"])
    candidate_request.add_argument("--branch", default="main")
    candidate_request.add_argument("--replacement-candidate")
    candidate_request.add_argument("--replacement-memory")
    commands.add_parser("install-git-hook")

    graph = commands.add_parser("graph-neighbors")
    graph.add_argument("entity")
    graph.add_argument("--branch", default="main")
    graph.add_argument("--limit", type=int, default=20)
    source = commands.add_parser("source")
    source.add_argument("ids", nargs="+")

    context = commands.add_parser("context")
    context.add_argument("id")
    context.add_argument("--branch")
    context.add_argument("--before", type=int, default=3)
    context.add_argument("--after", type=int, default=3)
    context.add_argument("--include-source", action="store_true")

    project_source = commands.add_parser("project-source")
    project_source.add_argument("path")
    project_source.add_argument("--expected-hash")

    timeline = commands.add_parser("timeline")
    timeline.add_argument("--branch", default="main")
    timeline.add_argument("--limit", type=int, default=50)
    timeline.add_argument("--kind", action="append", default=[])

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--branch", default="main")
    snapshot.add_argument("--parent")
    snapshot.add_argument("--track", action="append")

    snapshot_prune = commands.add_parser("snapshot-prune")
    snapshot_prune.add_argument("--id", action="append", required=True)
    snapshot_prune.add_argument("--branch", default="main")

    resume = commands.add_parser("resume")
    resume.add_argument("--branch", default="main")
    resume.add_argument("--budget", type=int, default=1500)
    resume.add_argument("--query", default="resume current goal constraints decisions and open tasks")
    resume.add_argument("--text-only", action="store_true")

    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("--agent")
    capabilities.add_argument("--supplied", type=_json_object)

    ingest = commands.add_parser("ingest")
    ingest.add_argument("agent", choices=["claude-code", "codex", "opencode", "openhands", "opencode-openhands"])
    ingest.add_argument("event", type=_json_object)
    ingest.add_argument("--branch", default="main")
    ingest.add_argument("--session")

    prompt = commands.add_parser("prompt")
    prompt.add_argument("--budget", type=int, required=True)
    prompt.add_argument("--branch", default="main")
    prompt.add_argument("--query", default="")
    prompt.add_argument("--recent", type=int, default=8)

    consolidate = commands.add_parser("consolidate")
    consolidate.add_argument("--branch", default="main")

    compact = commands.add_parser("compact")
    compact.add_argument("--branch", default="main")
    compact.add_argument("--keep-recent", type=int, default=8)
    compact.add_argument("--summary-budget", type=int, default=600)

    hook = commands.add_parser("hook")
    hook.add_argument("--agent", required=True, choices=["claude-code", "codex", "opencode", "openhands"])
    hook.add_argument("--branch", default="main")
    hook.add_argument("--budget", type=int, default=1500)
    hook.add_argument("--global", dest="global_scope", action="store_true")

    install = commands.add_parser("install-hooks")
    install.add_argument("agent", choices=["claude-code", "codex", "opencode", "openhands"])
    install.add_argument("--branch", default="main")
    install.add_argument("--budget", type=int, default=1500)
    install.add_argument("--global", dest="global_scope", action="store_true")
    install.add_argument("--profile", help="bundled model profile or custom")
    install.add_argument("--context-window", type=int)
    install.add_argument("--snapshot-ratio", type=float)
    install.add_argument("--compact-ratio", type=float)
    install.add_argument("--handoff-ratio", type=float)
    install.add_argument("--hard-limit-ratio", type=float)
    install.add_argument("--handoff-tokens", type=int)
    install.add_argument("--reserve-tokens", type=int)
    install.add_argument("--min-action-events", type=int)

    code_index = commands.add_parser("code-index")
    code_index.add_argument("--force", action="store_true")

    code_search = commands.add_parser("code-search")
    code_search.add_argument("query")
    code_search.add_argument("--limit", type=int, default=20)

    code_context = commands.add_parser("code-context")
    code_context.add_argument("symbol")

    code_impact = commands.add_parser("code-impact")
    code_impact.add_argument("symbol")
    code_impact.add_argument("--depth", type=int, default=3)

    output_views = commands.add_parser("output-views")
    output_views.add_argument("event_id")

    usage = commands.add_parser("usage")
    usage.add_argument("--branch", default="main")
    usage.add_argument("--session")

    commands.add_parser("context-profiles")

    budget_policy = commands.add_parser("budget-policy")
    budget_policy.add_argument("--branch", default="main")
    budget_policy.add_argument("--agent")
    budget_policy.add_argument("--profile")
    budget_policy.add_argument("--handoff-tokens", type=int)
    budget_policy.add_argument("--reserve-tokens", type=int)
    budget_policy.add_argument("--context-window", type=int)
    budget_policy.add_argument("--snapshot-ratio", type=float)
    budget_policy.add_argument("--compact-ratio", type=float)
    budget_policy.add_argument("--handoff-ratio", type=float)
    budget_policy.add_argument("--hard-limit-ratio", type=float)
    budget_policy.add_argument("--min-action-events", type=int)

    governor = commands.add_parser("governor")
    governor.add_argument("--branch", default="main")
    governor.add_argument("--session")
    governor.add_argument("--agent")
    governor.add_argument("--apply", action="store_true")

    task_start = commands.add_parser("task-start")
    task_start.add_argument("key")
    task_start.add_argument("title")
    task_start.add_argument("--parent-branch", default="main")
    task_start.add_argument("--parent-task")
    task_start.add_argument("--session")

    task_status = commands.add_parser("task-status")
    task_status.add_argument("key")
    task_status.add_argument("status", choices=["active", "blocked", "completed", "cancelled"])
    task_status.add_argument("--note", default="")
    task_status.add_argument("--session")

    task_resume = commands.add_parser("task-resume")
    task_resume.add_argument("key")
    task_resume.add_argument("--budget", type=int, default=1500)
    task_resume.add_argument("--query")
    task_resume.add_argument("--text-only", action="store_true")

    task_list = commands.add_parser("task-list")
    task_list.add_argument("--status", choices=["active", "blocked", "completed", "cancelled"])

    findings = commands.add_parser("findings")
    findings.add_argument("--acknowledged", action="store_true")

    finding_request = commands.add_parser("finding-ack-request")
    finding_request.add_argument("finding_id")
    finding_request.add_argument("--branch", default="main")

    policy_request = commands.add_parser("policy-request")
    policy_request.add_argument("policy", type=_json_object)
    policy_request.add_argument("--branch", default="main")

    commands.add_parser("doctor")
    commands.add_parser("verify")

    api = commands.add_parser("serve")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8765)
    commands.add_parser("mcp")

    physical = commands.add_parser("physical-plan")
    physical.add_argument("candidates", help="JSON array of physical-memory candidates")
    physical.add_argument("--budget-bytes", required=True, type=int)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("tasks", help="JSON file containing an array of evaluation tasks")
    evaluate.add_argument("--resume-budget", type=int, default=1500)
    evaluate.add_argument("--minimum", type=float)

    evaluate_extraction = commands.add_parser("evaluate-extraction")
    evaluate_extraction.add_argument("corpus")
    evaluate_extraction.add_argument("--minimum-precision", type=float)
    evaluate_runner = commands.add_parser("evaluate-runner")
    evaluate_runner.add_argument("tasks", help="JSON file containing task-level evaluation tasks")
    evaluate_runner.add_argument(
        "--runner-command",
        type=_json_array,
        required=True,
        help='JSON argv array, for example ["python","runner.py"]',
    )
    evaluate_runner.add_argument("--runner-timeout", type=float, default=300)
    evaluate_runner.add_argument("--resume-budget", type=int, default=1500)
    evaluate_runner.add_argument("--minimum", type=float)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "uninstall":
        project_root = resolve_runtime_project(args.project_root)
        delete_data = bool(args.delete_data)
        if (
            not args.delete_data
            and not args.keep_data
            and args.scope == "project"
            and sys.stdin.isatty()
        ):
            delete_data = confirm_data_deletion()
        _print(
            run_uninstall(
                project_root,
                agents=tuple(args.agent),
                scope=args.scope,
                remove_hook_adapters=False if args.without_hooks else None,
                remove_mcp=False if args.without_mcp else None,
                delete_data=delete_data,
                dry_run=args.dry_run,
            )
        )
        return 0
    if args.command == "setup":
        project_root = resolve_runtime_project(args.project_root)
        detections = detect_agents(project_root)
        if args.yes:
            agents = tuple(args.agent) or tuple(
                item.id for item in detections if item.detected
            )
            plugins = (
                ("semantic-local", "knowledge-graph", "nuextract-local")
                if args.all_plugins else tuple(args.plugin)
            )
            with_mcp = bool(args.with_mcp)
            scope = args.scope or "project"
            enable_extraction = bool(args.enable_extraction)
        else:
            explicit = (
                args.agent or args.plugin or args.all_plugins or args.without_hooks
                or args.scope or args.with_mcp or args.skip_plugin_install
                or args.enable_extraction or args.dry_run
            )
            if explicit:
                agents = tuple(args.agent)
                plugins = (
                    ("semantic-local", "knowledge-graph", "nuextract-local")
                    if args.all_plugins else tuple(args.plugin)
                )
                with_mcp = bool(args.with_mcp)
                scope = args.scope or "project"
                enable_extraction = bool(args.enable_extraction)
            elif sys.stdin.isatty():
                (
                    agents, plugins, with_mcp, scope, enable_extraction
                ) = select_interactively(detections)
            else:
                raise ValueError(
                    "setup requires --yes or explicit --agent/--plugin options "
                    "when stdin is not interactive"
                )
        _print(
            run_setup(
                project_root,
                agents=agents,
                plugins=plugins,
                scope=scope,
                install_hook_adapters=not args.without_hooks,
                install_mcp=with_mcp,
                install_plugins=not args.skip_plugin_install,
                enable_extraction=enable_extraction,
                source_root=args.source_root,
                dry_run=args.dry_run,
            )
        )
        return 0
    if args.command == "install-hooks":
        _print(
            install_hooks(
                args.agent,
                args.project_root,
                branch_id=args.branch,
                token_budget=args.budget,
                global_scope=args.global_scope,
                profile=args.profile,
                context_window_tokens=args.context_window,
                snapshot_ratio=args.snapshot_ratio,
                compact_ratio=args.compact_ratio,
                handoff_ratio=args.handoff_ratio,
                hard_limit_ratio=args.hard_limit_ratio,
                recommended_handoff_tokens=args.handoff_tokens,
                reserve_tokens=args.reserve_tokens,
                min_action_interval_events=args.min_action_events,
            )
        )
        return 0
    if args.command == "install-git-hook":
        project_root = resolve_runtime_project(args.project_root)
        _print(install_git_precommit(project_root))
        return 0
    if args.command == "hook":
        value = _hook_json_input(sys.stdin)
        if args.global_scope:
            project_root = resolve_hook_project(value)
            database = resolve_project_database(project_root)
        else:
            project_root = resolve_runtime_project(args.project_root)
            database = resolve_runtime_database(args.db, project_root)
        service = MemoryService(database, project_root=project_root)
        try:
            _print(process_hook(
                service,
                args.agent,
                value,
                branch_id=args.branch,
                token_budget=args.budget,
            ))
        finally:
            service.close()
        return 0
    project_root = resolve_runtime_project(args.project_root)
    database = resolve_runtime_database(args.db, project_root)
    missing_database = (
        WitnessRegistry().known_project_database_missing(project_root)
        if args.command == "init" else ()
    )
    service = MemoryService(database, project_root=project_root)
    try:
        if args.command == "init":
            initialized = service.initialize_project()
            for finding in missing_database:
                service.store.record_security_finding(
                    "known_project_database_missing",
                    incident_key=(
                        "known_project_database_missing:"
                        + str(finding["project_instance_id"])
                    ),
                    details=dict(finding),
                )
            _print({"database": str(service.store.path), **initialized})
        elif args.command == "session-start":
            _print({"id": service.store.start_session(args.agent, branch_id=args.branch, capabilities=args.capabilities)})
        elif args.command == "branch-create":
            _print({"id": service.store.create_branch(args.id, parent_id=args.parent, fork_event_seq=args.fork_seq)})
        elif args.command == "append":
            _print(service.append_event(kind=args.kind, content=args.content, role=args.role, branch_id=args.branch, session_id=args.session, payload=args.payload, files=args.file))
        elif args.command == "artifact":
            path = Path(args.path)
            _print(service.store.append_artifact(name=args.name or path.name, data=path.read_bytes(), mime_type=args.mime, branch_id=args.branch, session_id=args.session))
        elif args.command == "block-set":
            _print(service.store.set_active_block(args.name, args.content, branch_id=args.branch, session_id=args.session, source_event_ids=args.source))
        elif args.command == "derive":
            _print(service.derive_memory(memory_type=args.memory_type, content=args.content, summary=args.summary, source_event_ids=args.source, files=args.file, branch_id=args.branch, risk=args.risk, retrieval_cost=args.cost, supersedes_id=args.supersedes, valid_from=args.valid_from, valid_to=args.valid_to, temporal_expression=args.temporal_expression))
        elif args.command == "search":
            _print(service.search(query=args.query, branch_id=args.branch, memory_types=tuple(args.type), file=args.file, since=args.since, until=args.until, limit=args.limit, exact=args.exact, include_events=not args.no_events, semantic=not args.no_semantic, include_staleness=args.staleness, valid_at=args.valid_at, known_at=args.known_at, current=args.current, include_unknown_validity=args.include_unknown_validity, history=args.history))
        elif args.command == "stale":
            _print(service.stale(branch_id=args.branch, memory_id=args.id, file=args.file, threshold=args.threshold))
        elif args.command == "precheck":
            report = service.precheck(
                branch_id=args.branch,
                files=args.file,
                staged=args.staged,
                command=args.candidate_command,
            )
            _print(report)
            if args.strict and report.blocked:
                raise SystemExit(2)
        elif args.command == "extraction-status":
            _print(service.extraction.status())
        elif args.command == "extraction-worker":
            _print(service.extraction.run_worker())
        elif args.command == "extraction-process":
            _print(service.extraction.process_backlog(limit=args.limit))
        elif args.command == "extraction-retry":
            _print(service.extraction.retry_failures(limit=args.limit))
        elif args.command == "extraction-reprocess":
            _print(service.extraction.reprocess(
                ExtractorConfig(**args.config), limit=args.limit
            ))
        elif args.command == "extraction-candidates":
            _print(service.store.list_extraction_candidates(status=args.status))
        elif args.command == "candidate-request":
            _print(service.request_candidate_transition(
                args.candidate_id,
                args.action,
                branch_id=args.branch,
                replacement_candidate_id=args.replacement_candidate,
                replacement_memory_id=args.replacement_memory,
            ))
        elif args.command == "graph-neighbors":
            _print(service.knowledge_neighbors(
                args.entity, branch_id=args.branch, limit=args.limit
            ))
        elif args.command == "source":
            identifiers = _identifier_list(args.ids)
            _print(
                service.exact_source(identifiers[0])
                if len(identifiers) == 1
                else service.exact_sources(identifiers)
            )
        elif args.command == "context":
            _print(service.context_around(
                args.id,
                branch_id=args.branch,
                before=args.before,
                after=args.after,
                include_source=args.include_source,
            ))
        elif args.command == "project-source":
            _print(service.project_source(args.path, expected_hash=args.expected_hash))
        elif args.command == "timeline":
            _print(service.retrieval.timeline(branch_id=args.branch, limit=args.limit, kinds=args.kind))
        elif args.command == "snapshot":
            _print(service.create_snapshot(branch_id=args.branch, parent_snapshot_id=args.parent, tracked_files=args.track))
        elif args.command == "snapshot-prune":
            _print(service.prune_snapshots(args.id, branch_id=args.branch))
        elif args.command == "resume":
            packet = service.resume(branch_id=args.branch, token_budget=args.budget, query=args.query)
            print(packet.text) if args.text_only else _print(packet)
        elif args.command == "capabilities":
            _print(service.capabilities(args.agent, args.supplied))
        elif args.command == "ingest":
            _print(service.ingest_native(args.agent, args.event, branch_id=args.branch, session_id=args.session))
        elif args.command == "prompt":
            _print(service.prompts.assemble(token_budget=args.budget, branch_id=args.branch, query=args.query, recent_event_count=args.recent))
        elif args.command == "consolidate":
            _print(service.consolidate(branch_id=args.branch))
        elif args.command == "compact":
            _print(service.compact(branch_id=args.branch, keep_recent_groups=args.keep_recent, summary_budget=args.summary_budget))
        elif args.command == "code-index":
            _print(service.code.build(force=args.force))
        elif args.command == "code-search":
            _print(service.code.search(args.query, limit=args.limit))
        elif args.command == "code-context":
            _print(service.code.context(args.symbol))
        elif args.command == "code-impact":
            _print(service.code.impact(args.symbol, depth=args.depth))
        elif args.command == "output-views":
            _print(service.store.list_tool_output_views(args.event_id))
        elif args.command == "usage":
            _print(service.usage.report(branch_id=args.branch, session_id=args.session))
        elif args.command == "context-profiles":
            _print(service.context_limits.builtins)
        elif args.command == "budget-policy":
            if args.agent:
                path, policy = service.context_limits.configure_agent(
                    args.agent,
                    profile=args.profile,
                    overrides={
                        "context_window_tokens": args.context_window,
                        "snapshot_ratio": args.snapshot_ratio,
                        "compact_ratio": args.compact_ratio,
                        "handoff_ratio": args.handoff_ratio,
                        "hard_limit_ratio": args.hard_limit_ratio,
                        "recommended_handoff_tokens": args.handoff_tokens,
                        "reserve_tokens": args.reserve_tokens,
                        "min_action_interval_events": args.min_action_events,
                    },
                )
                _print({"path": str(path), "policy": policy})
            else:
                _print(service.store.set_budget_policy(
                    branch_id=args.branch,
                    context_window_tokens=args.context_window or 200_000,
                    snapshot_ratio=args.snapshot_ratio or 0.45,
                    compact_ratio=args.compact_ratio or 0.60,
                    handoff_ratio=args.handoff_ratio or 0.75,
                    hard_limit_ratio=args.hard_limit_ratio or 0.90,
                    min_action_interval_events=(
                        20 if args.min_action_events is None else args.min_action_events
                    ),
                ))
        elif args.command == "governor":
            if args.apply:
                _print(service.governor.evaluate_and_apply(
                    branch_id=args.branch, session_id=args.session, agent=args.agent
                ))
            else:
                _print(service.governor.decide(
                    branch_id=args.branch, session_id=args.session, agent=args.agent
                ))
        elif args.command == "task-start":
            _print(service.tasks.start(
                args.key,
                args.title,
                parent_branch=args.parent_branch,
                parent_task_key=args.parent_task,
                session_id=args.session,
            ))
        elif args.command == "task-status":
            _print(service.tasks.set_status(
                args.key, args.status, note=args.note, session_id=args.session
            ))
        elif args.command == "task-resume":
            packet = service.tasks.resume(
                args.key, token_budget=args.budget, query=args.query
            )
            print(packet.text) if args.text_only else _print(packet)
        elif args.command == "task-list":
            _print(service.tasks.list(status=args.status))
        elif args.command == "findings":
            values = service.store.list_security_findings()
            _print(
                [item for item in values if item["acknowledged"]]
                if args.acknowledged else values
            )
        elif args.command == "finding-ack-request":
            _print(service.request_finding_acknowledgement(
                args.finding_id, branch_id=args.branch
            ))
        elif args.command == "policy-request":
            _print(service.request_policy_change(args.policy, branch_id=args.branch))
        elif args.command == "doctor":
            _print({
                **service.verify(),
                **service.security_status(),
                "extraction": service.extraction.status(),
            })
        elif args.command == "verify":
            result = service.verify()
            _print(result)
            return 0 if result["valid"] else 2
        elif args.command == "serve":
            serve(service, args.host, args.port)
        elif args.command == "mcp":
            serve_stdio(service)
        elif args.command == "physical-plan":
            values = json.loads(args.candidates)
            candidates = [PhysicalCandidate(placement=Placement(item.pop("placement")), **item) for item in values]
            _print(PhysicalMemoryGovernor().choose(candidates, memory_budget_bytes=args.budget_bytes))
        elif args.command == "evaluate":
            tasks = _evaluation_tasks(args.tasks)
            report = evaluate_policies(
                service,
                tasks,
                policies=[FullHistoryPolicy(), ResumePolicy(args.resume_budget)],
            )
            if args.minimum is not None:
                assert_resume_quality(report, args.minimum)
            _print(report)
        elif args.command == "evaluate-extraction":
            if service.extraction.extractor is None or service.extraction.config is None:
                raise ValueError("no extractor plugin/configuration is available")
            report = evaluate_extractor(
                service.extraction.extractor,
                service.extraction.config,
                args.corpus,
            )
            if (
                args.minimum_precision is not None
                and report["overall"]["precision"] < args.minimum_precision
            ):
                raise AssertionError(
                    "extraction precision "
                    f"{report['overall']['precision']:.4f} is below "
                    f"{args.minimum_precision:.4f}"
                )
            _print(report)
        elif args.command == "evaluate-runner":
            report = evaluate_with_runner(
                service,
                _evaluation_tasks(args.tasks),
                SubprocessTaskRunner(args.runner_command, timeout_seconds=args.runner_timeout),
                policies=[FullHistoryPolicy(), ResumePolicy(args.resume_budget)],
            )
            if args.minimum is not None:
                assert_task_quality(report, args.minimum)
            _print(report)
        return 0
    finally:
        service.close()


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
