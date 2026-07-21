from __future__ import annotations

import argparse
import ast
import inspect
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from joiny_mnemonic.adapters import PUBLIC_CAPABILITY_FLAGS, adapter_capabilities
from joiny_mnemonic.policy_contract import PUBLIC_POLICY_FLAGS
from joiny_mnemonic.provenance import (
    INTERNAL,
    ORIGIN_CHANNELS,
    ORIGIN_EVIDENCE_TYPES,
    SETTLEMENT_REQUEST_OPERATION,
    origin_evidence_type,
)
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.transition_rules import (
    CANDIDATE_FLOW,
    FINDING_FLOW,
    SETTLEMENT_FLOW,
    WORKSTREAM_FLOW,
)


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Event:
    origin_channel: str
    role: str | None = None
    origin_adapter: str | None = None
    payload: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", self.payload or {})


def _flow_values(flow: dict[str, Iterable[str]]) -> set[str]:
    return set(flow) | {target for targets in flow.values() for target in targets}


def _origin_producers() -> set[str]:
    return {
        origin_evidence_type(_Event("public_api")),
        origin_evidence_type(_Event("host_hook", "user", "claude-code")),
        origin_evidence_type(_Event(
            "host_hook", "assistant", "codex", {"hook_event_name": "Stop"}
        )),
        origin_evidence_type(_Event(
            INTERNAL, payload={"operation": "policy_bootstrapped"}
        )),
        origin_evidence_type(_Event(
            INTERNAL,
            payload={
                "operation": SETTLEMENT_REQUEST_OPERATION,
                "requested_by": "operator",
            },
        )),
        origin_evidence_type(_Event(
            INTERNAL,
            payload={
                "operation": SETTLEMENT_REQUEST_OPERATION,
                "requested_by": "agent",
            },
        )),
    }


def _policy_producers() -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(MemoryService.initialize_project)))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    return literals & set(PUBLIC_POLICY_FLAGS)


def _values_with_runtime_consumers(
    values: set[str], declaration_file: str
) -> set[str]:
    consumed = set()
    for value in values:
        for path in (ROOT / "src" / "joiny_mnemonic").glob("*.py"):
            if path.name == declaration_file:
                continue
            if value in path.read_text(encoding="utf-8"):
                consumed.add(value)
                break
    return consumed


def contract_errors(extra: dict[str, set[str]] | None = None) -> list[str]:
    flows = (WORKSTREAM_FLOW, CANDIDATE_FLOW, FINDING_FLOW, SETTLEMENT_FLOW)
    expected = {
        "origins": set(ORIGIN_EVIDENCE_TYPES),
        "statuses": set().union(*(_flow_values(flow) for flow in flows)),
        "modes": set(ORIGIN_CHANNELS),
        "capability_flags": set(PUBLIC_CAPABILITY_FLAGS),
        "policy_flags": set(PUBLIC_POLICY_FLAGS),
    }
    for category, values in (extra or {}).items():
        expected.setdefault(category, set()).update(values)
    produced = {
        "origins": _origin_producers(),
        "statuses": set().union(*(_flow_values(flow) for flow in flows)),
        "modes": {"public_api", "host_hook", "internal", "legacy_untrusted"},
        "capability_flags": set(adapter_capabilities("claude-code")) - {"agent"},
        "policy_flags": _policy_producers(),
    }
    consumers = {
        "origins": "validate_transition",
        "statuses": "validate_transition",
        "modes": "origin_evidence_type",
        "capability_flags": "MemoryService._agent_capabilities",
        "policy_flags": "MemoryService.__init__/SettlementSurface",
    }
    per_value_consumers = {
        "capability_flags": _values_with_runtime_consumers(
            expected["capability_flags"], "adapters.py"
        ),
        "policy_flags": _values_with_runtime_consumers(
            expected["policy_flags"], "policy_contract.py"
        ),
    }
    errors = []
    for category, values in expected.items():
        missing = values - produced.get(category, set())
        unexpected = produced.get(category, set()) - values
        if missing:
            errors.append(f"{category}: no producer for {sorted(missing)}")
        if unexpected:
            errors.append(f"{category}: undeclared producer values {sorted(unexpected)}")
        if not consumers.get(category):
            errors.append(f"{category}: no reachable consumer")
        missing_consumers = values - per_value_consumers.get(category, values)
        if missing_consumers:
            errors.append(
                f"{category}: no runtime consumer for {sorted(missing_consumers)}"
            )
    return errors


def _metrics(path: Path) -> dict[str, int]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        "physical_lines": len(source.splitlines()),
        "functions_methods": sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(tree)
        ),
        "classes": sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)),
    }


def complexity_errors() -> list[str]:
    baseline = json.loads(
        (ROOT / "quality" / "complexity-baseline.json").read_text(encoding="utf-8")
    )
    errors = []
    for relative, limits in baseline["files"].items():
        actual = _metrics(ROOT / relative)
        for metric, limit in limits.items():
            if actual[metric] > limit:
                errors.append(
                    f"{relative} {metric}: {actual[metric]} exceeds baseline {limit}"
                )
    exempt = set(baseline["files"]) | set(
        baseline["preexisting_large_runtime_modules"]
    )
    maximum = int(baseline["new_runtime_module_max_physical_lines"])
    for path in (ROOT / "src" / "joiny_mnemonic").glob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if relative not in exempt and lines > maximum:
            errors.append(f"{relative} physical_lines: {lines} exceeds new-module {maximum}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gate", choices=("contract", "complexity", "all"))
    parser.add_argument("--inject-dead", metavar="CATEGORY:VALUE")
    args = parser.parse_args()
    extra: dict[str, set[str]] = {}
    if args.inject_dead:
        category, separator, value = args.inject_dead.partition(":")
        if not separator or not category or not value:
            parser.error("--inject-dead requires CATEGORY:VALUE")
        extra = {category: {value}}
    errors = []
    if args.gate in {"contract", "all"}:
        errors.extend(contract_errors(extra))
    if args.gate in {"complexity", "all"}:
        errors.extend(complexity_errors())
    if errors:
        print("\n".join(f"FAIL: {error}" for error in errors))
        return 1
    print(f"PASS: stage1 {args.gate} gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
