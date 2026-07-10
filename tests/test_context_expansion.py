from __future__ import annotations

import unittest

from joiny_mnemonic.cli import _identifier_list, build_parser
from joiny_mnemonic.mcp import MCPServer
from joiny_mnemonic.service import MemoryService


class ContextExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:")

    def tearDown(self) -> None:
        self.service.close()

    def test_event_context_compact_and_exact_preserve_order_roles_and_kinds(self) -> None:
        first = self.service.store.append_event(
            kind="message", role="user", content="first event"
        )
        center = self.service.store.append_event(
            kind="message", role="assistant", content="center event"
        )
        last = self.service.store.append_event(
            kind="artifact", role=None, content="last event"
        )

        compact = self.service.context_around(center.id, before=1, after=1)
        self.assertEqual(compact.events, ())
        self.assertEqual(
            tuple(item.id for item in compact.index),
            (first.id, center.id, last.id),
        )
        self.assertEqual(
            tuple((item.kind, item.role) for item in compact.index),
            (("message", "user"), ("message", "assistant"), ("artifact", None)),
        )
        self.assertEqual(compact.primary_event_id, center.id)
        self.assertTrue(next(item for item in compact.index if item.id == center.id).is_source)

        exact = self.service.context_around(
            center.id, before=1, after=1, include_source=True
        )
        self.assertEqual(exact.index, ())
        self.assertEqual(tuple(event.id for event in exact.events), (first.id, center.id, last.id))
        self.assertEqual(exact.events[1], center)

    def test_memory_with_several_sources_and_snapshot_replay_id(self) -> None:
        first = self.service.store.append_event(
            kind="message", role="user", content="source one"
        )
        self.service.store.append_event(kind="message", content="unrelated middle")
        second = self.service.store.append_event(
            kind="message", role="assistant", content="source two"
        )
        memory = self.service.derive_memory(
            memory_type="decision",
            content="Use both pieces of evidence.",
            source_event_ids=[second.id, first.id],
        )

        window = self.service.context_around(
            memory.id, before=0, after=0, include_source=True
        )
        self.assertEqual(window.source_event_ids, (second.id, first.id))
        self.assertEqual(window.primary_event_id, first.id)
        self.assertEqual(tuple(event.id for event in window.events), (first.id, second.id))

        derivation = next(
            event
            for event in self.service.store.query_events(kinds=["state"])
            if event.payload.get("memory_id") == memory.id
        )
        replay = self.service.context_around(
            f"replay:{derivation.id}", before=0, after=0, include_source=True
        )
        self.assertEqual(replay.source_event_ids, (second.id, first.id))
        self.assertEqual(tuple(event.id for event in replay.events), (first.id, second.id))

    def test_tool_call_output_is_atomic_and_orphan_output_is_omitted(self) -> None:
        call = self.service.store.append_event(
            kind="tool_call",
            role="assistant",
            content="run tests",
            payload={"tool_call_id": "call-1"},
        )
        self.service.store.append_event(
            kind="message", role="assistant", content="between"
        )
        output = self.service.store.append_event(
            kind="tool_output",
            role="tool",
            content="\n".join(f"passing test {index}" for index in range(200)),
            payload={"tool_call_id": "call-1"},
        )
        orphan = self.service.store.append_event(
            kind="tool_output",
            role="tool",
            content="orphan",
            payload={"tool_call_id": "missing"},
        )
        _, views = self.service.reduce_tool_output(output)

        window = self.service.context_around(
            views[0].id, before=0, after=0, include_source=True
        )
        self.assertEqual(tuple(event.id for event in window.events), (call.id, output.id))
        self.assertNotIn(orphan.id, {event.id for event in window.events})
        self.assertEqual(window.group_count, 1)

    def test_branch_lineage_honors_fork_cursor(self) -> None:
        visible_parent = self.service.store.append_event(
            kind="message", content="visible parent"
        )
        self.service.store.create_branch(
            "child", parent_id="main", fork_event_seq=visible_parent.seq
        )
        hidden_parent = self.service.store.append_event(
            kind="message", content="hidden parent"
        )
        child = self.service.store.append_event(
            kind="message", content="child event", branch_id="child"
        )

        window = self.service.context_around(
            child.id,
            branch_id="child",
            before=20,
            after=20,
            include_source=True,
        )
        ids = tuple(event.id for event in window.events)
        self.assertEqual(ids, (visible_parent.id, child.id))
        self.assertNotIn(hidden_parent.id, ids)
        with self.assertRaises(ValueError):
            self.service.context_around(
                hidden_parent.id, branch_id="child", include_source=True
            )

    def test_batch_source_preserves_single_source_and_mcp_contract(self) -> None:
        source = self.service.store.append_event(kind="message", content="source")
        memory = self.service.derive_memory(
            memory_type="fact", content="fact", source_event_ids=[source.id]
        )

        self.assertEqual(self.service.exact_source(memory.id), [source])
        batch = self.service.exact_sources([source.id, memory.id, source.id])
        self.assertEqual(tuple(item.id for item in batch), (source.id, memory.id))
        self.assertEqual(batch[1].events, (source,))

        server = MCPServer(self.service)
        self.assertEqual(server._call_tool("memory_source", {"id": memory.id}), [source])
        batch_mcp = server._call_tool(
            "memory_source", {"ids": [source.id, memory.id]}
        )
        self.assertEqual(tuple(item.id for item in batch_mcp), (source.id, memory.id))
        context = server._call_tool(
            "memory_context",
            {"id": memory.id, "before": 0, "after": 0},
        )
        self.assertEqual(context.source_event_ids, (source.id,))
        with self.assertRaises(ValueError):
            server._call_tool(
                "memory_source", {"id": memory.id, "ids": [memory.id]}
            )

    def test_cli_accepts_repeated_ids_and_json_array(self) -> None:
        parser = build_parser()
        repeated = parser.parse_args(["source", "evt_one", "mem_two"])
        self.assertEqual(_identifier_list(repeated.ids), ["evt_one", "mem_two"])
        encoded = parser.parse_args(["source", '["evt_one","mem_two"]'])
        self.assertEqual(_identifier_list(encoded.ids), ["evt_one", "mem_two"])

    def test_context_bounds_are_hard_and_output_is_chronological(self) -> None:
        events = [
            self.service.store.append_event(kind="message", content=f"event {index}")
            for index in range(5)
        ]
        for name, values in (
            ("before", {"before": -1}),
            ("before", {"before": 21}),
            ("after", {"after": -1}),
            ("after", {"after": 21}),
        ):
            with self.subTest(name=name, values=values), self.assertRaises(ValueError):
                self.service.context_around(events[2].id, **values)

        window = self.service.context_around(
            events[2].id, before=2, after=2, include_source=True
        )
        self.assertEqual(
            tuple(event.seq for event in window.events),
            tuple(sorted(event.seq for event in window.events)),
        )


if __name__ == "__main__":
    unittest.main()