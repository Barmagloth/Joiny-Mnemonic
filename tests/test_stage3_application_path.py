from __future__ import annotations

import unittest
from pathlib import Path

from scripts.stage3_surface_audit import (
    calls_in_source,
    declared_store_reads,
    direct_store_writes,
)


ROOT = Path(__file__).resolve().parents[1]


class Stage3SurfaceAuditTest(unittest.TestCase):
    def test_unknown_direct_store_call_fails_closed(self) -> None:
        writes = calls_in_source(
            "def route(service):\n    service.store.future_write(value=1)\n",
            path="fixture.py",
        )
        self.assertEqual(
            [(item.path, item.method) for item in writes],
            [("fixture.py", "future_write")],
        )

    def test_alias_bound_method_and_dynamic_access_fail_closed(self) -> None:
        fixtures = (
            "def route(service):\n    s = service.store\n    s.future_write()\n",
            "def route(service):\n    write = service.store.future_write\n    write()\n",
            "def route(service):\n    getattr(service.store, 'future_write')()\n",
            "def route(service):\n    getattr(service, 'store').future_write()\n",
            "def route(service, name):\n    getattr(service, name).future_write()\n",
            "def route(service):\n    service.__getattribute__('store').future_write()\n",
            "def route(service):\n    vars(service)['store'].future_write()\n",
            "def route(service):\n    service.__dict__['store'].future_write()\n",
            "def route(service):\n    service.__dict__.get('store').future_write()\n",
            "def route(service):\n    service.store['future_write']()\n",
            "def route(service, other):\n    service.store.path = other\n",
            "def route(service):\n    service.store.path.unlink()\n",
            "def route(service):\n    consume(service.store)\n",
            "def route(service):\n    ga = getattr\n    ga(service, 'store').future_write()\n",
            "def route(service):\n    vr = vars\n    vr(service)['store'].future_write()\n",
            "import builtins\ndef route(service):\n    builtins.getattr(service, 'store').future_write()\n",
            "import operator\ndef route(service):\n    operator.attrgetter('store')(service).future_write()\n",
            "import builtins as bi\ndef route(service):\n    svc = service\n    ga = bi.getattr\n    ga(svc, 'store').future_write()\n",
            "from operator import attrgetter as ag\ndef route(service):\n    pick = ag\n    pick('store')(service).future_write()\n",
            "def route(service):\n    d = vars(service)\n    d['store'].future_write()\n",
            "def route(service):\n    vars(service).get('store').future_write()\n",
            "def route(service):\n    d = service.__dict__\n    d['store'].future_write()\n",
            "def route(service):\n    ga = service.__getattribute__\n    ga('store').future_write()\n",
            "import operator\ndef route(service):\n    pick = operator.attrgetter('store')\n    pick(service).future_write()\n",
            "def route(service):\n    eval('service.store.future_write()')\n",
            "def route(service):\n    exec('service.store.future_write()')\n",
            "def route(service):\n    ga: object = getattr\n    ga(service, 'store').future_write()\n",
            "def route(service):\n    (svc := service)\n    svc.store.future_write()\n",
            "def route(service):\n    (svc,) = (service,)\n    svc.store.future_write()\n",
            "def route(service):\n    object.__getattribute__(service, 'store').future_write()\n",
            "def route(service):\n    service.__dict__.pop('store').future_write()\n",
            "def route(service):\n    dict.get(vars(service), 'store').future_write()\n",
            "def route(service):\n    consume(vars(service))\n",
            "def route(service):\n    consume(service.__getattribute__)\n",
            "def route(service):\n    eval('service.store.' + 'future_write()')\n",
            "def route(service):\n    exec(b'service.store.future_write()')\n",
            "def route(service):\n    getattr(service, 'store').future_write()\n\ndef helper(getattr):\n    return getattr\n",
            "def route(service):\n    ga = object.__getattribute__\n    ga(service, 'store').future_write()\n",
            "def outer(service):\n    svc = service\n    ga = getattr\n    def route():\n        ga(svc, 'store').future_write()\n",
            "def route(service, ga=getattr):\n    ga(service, 'store').future_write()\n",
            "def route(service, svc=service):\n    svc.store.future_write()\n",
            "def route(service):\n    svc = service\n    exec('svc.store.future_write()')\n",
            "def route(service):\n    return (lambda svc=service: svc.store.future_write())()\n",
            "def route(service):\n    return (lambda ga=getattr: ga(service, 'store').future_write())()\n",
            "def route(service):\n    return (lambda run=exec: run('service.store.future_write()'))()\n",
            "def route(service):\n    ga = None\n    ga = getattr\n    ga(service, 'store').future_write()\n",
            "def route(service):\n    ga = getattr\n    ga(service, 'store').future_write()\n    ga = None\n",
            "class Handler:\n    def route(self):\n        getattr(self, 'service').store.future_write()\n",
            "class Handler:\n    def route(self):\n        self.__dict__['service'].store.future_write()\n",
            "def outer(service):\n    def route():\n        svc.store.future_write()\n    svc = service\n    route()\n",
            "def route(service):\n    (ga := getattr)(service, 'store').future_write()\n",
            "def route(service):\n    (ga := object.__getattribute__)(service, 'store').future_write()\n",
            "def route(service):\n    (svc := service).store.future_write()\n",
            "class Handler:\n    def route(self):\n        this = self\n        this.service.store.future_write()\n",
            "class Handler:\n    def route(self):\n        this = self\n        getattr(this, 'service').store.future_write()\n",
            "def route(service):\n    getattr(service, '__dict__')['store'].future_write()\n",
            "def route(service):\n    object.__getattribute__(service, '__dict__')['store'].future_write()\n",
            "class Handler:\n    def route(self):\n        getattr(self, '__dict__')['service'].store.future_write()\n",
            "def route(service):\n    ga = getattr\n    def inner(get=ga, svc=service):\n        get(svc, 'store').future_write()\n    ga = None\n    service = None\n    inner()\n",
            "def route(service):\n    ga = getattr\n    inner = lambda get=ga, svc=service: get(svc, 'store').future_write()\n    ga = None\n    service = None\n    inner()\n",
            "class Handler:\n    def route(self):\n        vars(self)['service'].store.future_write()\n",
            "class Handler:\n    def route(self):\n        self.__dict__.get('service').store.future_write()\n",
            "getattr = lambda *args: None\nfrom builtins import getattr\ngetattr(service, 'store').future_write()\n",
        )
        for source in fixtures:
            with self.subTest(source=source):
                self.assertTrue(calls_in_source(source, path="fixture.py"))

    def test_unrelated_reflection_is_not_a_store_violation(self) -> None:
        source = """
def route(request):
    getattr(request, 'method', None)
    getattr(request, 'store', None)
    request.store
    request.service.store.get_event('evt')
    vars(request)
    setattr(request, 'method', 'GET')
    eval('1 + 1')
    exec('answer = 2')
"""
        self.assertEqual(calls_in_source(source, path="fixture.py"), ())
        shadowed = """
def getattr(obj, field):
    return None
def route(request):
    return getattr(request, 'store')
"""
        self.assertEqual(calls_in_source(shadowed, path="fixture.py"), ())
        assigned = """
def route(service):
    getattr = lambda obj, name: None
    return getattr(service, 'store')
"""
        self.assertEqual(calls_in_source(assigned, path="fixture.py"), ())

    def test_branch_and_call_site_contexts_fail_closed(self) -> None:
        called_before_shadow = """def outer(service):
    ga = getattr
    def inner():
        ga(service, 'store').future_write()
    inner()
    ga = lambda *args: None
"""
        possible_unshadowed_branch = """def route(service, cond):
    ga = getattr
    if cond:
        ga = lambda *args: None
    ga(service, 'store').future_write()
"""
        called_after_shadow = """def outer(service):
    ga = getattr
    def inner():
        ga(service, 'store').future_write()
    ga = lambda *args: None
    inner()
"""
        possible_reflection_aliases = """class Handler:
    def route(self, cond):
        ga = getattr
        if cond:
            ga = vars
        ga(self)['service'].store.future_write()
"""
        possible_binding_kinds = """class Handler:
    def route(self, service, cond):
        target = service
        if cond:
            target = self
        target.service.store.future_write()
"""
        self.assertTrue(calls_in_source(called_before_shadow, path="fixture.py"))
        self.assertTrue(
            calls_in_source(possible_unshadowed_branch, path="fixture.py")
        )
        self.assertTrue(
            calls_in_source(possible_reflection_aliases, path="fixture.py")
        )
        self.assertTrue(
            calls_in_source(possible_binding_kinds, path="fixture.py")
        )
        self.assertEqual(
            calls_in_source(called_after_shadow, path="fixture.py"), ()
        )

    def test_literal_nesting_has_no_semantic_depth_bypass(self) -> None:
        payload = "service.store.future_write()"
        for _ in range(4):
            payload = f"exec({payload!r})"
        self.assertTrue(calls_in_source(payload, path="fixture.py"))

    def test_unrelated_duplicate_classes_do_not_ambiguate_store_mro(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "stage3_mro"
        self.assertEqual(declared_store_reads(fixture), frozenset({"lookup"}))

    def test_effective_override_and_foreign_decorator_are_not_reads(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "stage3_mro_override"
        self.assertEqual(declared_store_reads(fixture), frozenset())
        shadow = ROOT / "tests" / "fixtures" / "stage3_decorator_shadow"
        foreign = ROOT / "tests" / "fixtures" / "stage3_foreign_decorator"
        late = ROOT / "tests" / "fixtures" / "stage3_decorator_late_import"
        class_shadow = ROOT / "tests" / "fixtures" / "stage3_class_decorator_shadow"
        self.assertEqual(declared_store_reads(shadow), frozenset())
        self.assertEqual(declared_store_reads(foreign), frozenset())
        self.assertEqual(declared_store_reads(late), frozenset())
        self.assertEqual(declared_store_reads(class_shadow), frozenset())

    def test_mro_resolves_relative_and_fully_qualified_module_imports(self) -> None:
        relative = ROOT / "tests" / "fixtures" / "stage3_mro_module_import"
        qualified = ROOT / "tests" / "fixtures" / "stage3_mro_full_import"
        self.assertEqual(declared_store_reads(relative), frozenset({"lookup"}))
        self.assertEqual(declared_store_reads(qualified), frozenset({"lookup"}))
        foreign = ROOT / "tests" / "fixtures" / "stage3_mro_foreign"
        self.assertEqual(declared_store_reads(foreign), frozenset())
        nested = ROOT / "tests" / "fixtures" / "stage3_mro_nested_foreign"
        self.assertEqual(declared_store_reads(nested), frozenset())
        unresolved = ROOT / "tests" / "fixtures" / "stage3_mro_unresolved_first"
        parent = ROOT / "tests" / "fixtures" / "stage3_mro_parent_relative"
        self.assertEqual(declared_store_reads(unresolved), frozenset())
        self.assertEqual(declared_store_reads(parent), frozenset())

    def test_last_top_level_memory_store_binding_wins(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "stage3_last_memory_store"
        self.assertEqual(declared_store_reads(fixture), frozenset())
        assignment = ROOT / "tests" / "fixtures" / "stage3_last_memory_assignment"
        imported = ROOT / "tests" / "fixtures" / "stage3_last_memory_import"
        self.assertEqual(declared_store_reads(assignment), frozenset())
        self.assertEqual(declared_store_reads(imported), frozenset())

    def test_deleted_override_does_not_hide_effective_base_method(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "stage3_deleted_override"
        self.assertEqual(declared_store_reads(fixture), frozenset())

    def test_read_classification_comes_from_store_declarations(self) -> None:
        reads = declared_store_reads(ROOT)
        self.assertEqual(reads, frozenset({
            "assert_integrity", "get_active_blocks", "get_artifact", "get_event",
            "list_extraction_candidates", "list_memories", "list_security_findings",
            "list_settlement_candidates", "list_tool_output_views",
            "task_for_hook_session",
        }))
        self.assertEqual(
            calls_in_source(
                "def route(service):\n    service.store.get_event('evt')\n",
                path="fixture.py",
                read_methods=reads,
            ),
            (),
        )
    def test_current_direct_store_write_inventory_is_frozen(self) -> None:
        writes = direct_store_writes(ROOT)
        self.assertEqual(
            [(item.path, item.line, item.method) for item in writes],
            [
                ("src/joiny_mnemonic/api.py", 177, "start_session"),
                ("src/joiny_mnemonic/api.py", 185, "create_branch"),
                ("src/joiny_mnemonic/api.py", 197, "append_artifact"),
                ("src/joiny_mnemonic/api.py", 199, "set_active_block"),
                ("src/joiny_mnemonic/api.py", 238, "set_budget_policy"),
                ("src/joiny_mnemonic/cli.py", 679, "record_security_finding"),
                ("src/joiny_mnemonic/cli.py", 689, "start_session"),
                ("src/joiny_mnemonic/cli.py", 691, "create_branch"),
                ("src/joiny_mnemonic/cli.py", 696, "append_artifact"),
                ("src/joiny_mnemonic/cli.py", 698, "set_active_block"),
                ("src/joiny_mnemonic/cli.py", 852, "set_budget_policy"),
                ("src/joiny_mnemonic/hooks.py", 376, "hook_session"),
                ("src/joiny_mnemonic/hooks.py", 383, "bind_task_session"),
                ("src/joiny_mnemonic/hooks.py", 421, "append_host_events_once"),
                ("src/joiny_mnemonic/hooks.py", 496, "after_commit"),
                ("src/joiny_mnemonic/mcp.py", 500, "set_active_block"),
            ],
        )
