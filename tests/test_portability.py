"""Architecture guard (Verification item 8), gate-pin/tests/test_portability.py precedent.

Catches four one-line regressions that each look harmless in a diff but
reintroduce a scaffold bug: the removed hass.components proxy (S1), blocking
I/O smuggled into async code (S11/S12), services registered per-entry
instead of once in async_setup (S10), and a stale strings.json that custom
integrations don't actually read (S14).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "whatsapp_waha"

BLOCKING_CALL_NAMES = {"open", "read_text", "write_text", "read_bytes", "write_bytes"}

# async_add_executor_job / async_add_import_executor_job wrap a blocking call
# and run it off the event loop -- that's the sanctioned way to do this, so
# a blocking name appearing only as an *argument* to one of these doesn't count.
EXECUTOR_WRAPPERS = {"async_add_executor_job", "async_add_import_executor_job"}


def _iter_py_files():
    return sorted(INTEGRATION_DIR.rglob("*.py"))


def test_no_hass_components_proxy():
    """hass.components was removed in HA 2025.5.0 (S1) -- use direct imports instead."""
    offenders = []
    pattern = re.compile(r"\bhass\.components\b")
    for path in _iter_py_files():
        if pattern.search(path.read_text()):
            offenders.append(path.name)
    assert not offenders, f"hass.components is gone; found in: {offenders}"


def test_no_strings_json():
    """Custom integrations only read translations/en.json (S14) -- strings.json is dead weight."""
    assert not (INTEGRATION_DIR / "strings.json").exists()


def test_no_blocking_io_in_async_functions():
    """A blocking file call inside `async def` (not wrapped in an executor job) blocks the event loop."""
    offenders = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in EXECUTOR_WRAPPERS:
                    continue  # the call itself is fine; its *argument* call is examined separately below
                if name in BLOCKING_CALL_NAMES and not _is_wrapped_in_executor_job(node, call):
                    offenders.append(f"{path.name}:{node.name} calls {name}(...) directly")
    assert not offenders, "Blocking I/O found inside async code: " + "; ".join(offenders)


def _is_wrapped_in_executor_job(func_node: ast.AsyncFunctionDef, target_call: ast.Call) -> bool:
    """True if target_call's callee is passed *as a reference* (not invoked) to an
    executor-job call in the same function, e.g. `hass.async_add_executor_job(path.read_text)`."""
    if not isinstance(target_call.func, ast.Attribute):
        return False
    target_attr = target_call.func.attr

    for call in ast.walk(func_node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in EXECUTOR_WRAPPERS:
            continue
        for arg in call.args:
            if isinstance(arg, ast.Attribute) and arg.attr == target_attr:
                return True
    return False


def _find_service_register_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_register"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "services"
        ):
            calls.append(node)
    return calls


def test_services_registered_only_in_async_setup_not_async_setup_entry():
    """Services must be registered once in async_setup (S10), never per-entry in
    async_setup_entry -- and, since milestone 2 moved registration into
    services.py, never anywhere in __init__.py at all any more."""
    init_path = INTEGRATION_DIR / "__init__.py"
    services_path = INTEGRATION_DIR / "services.py"
    offenders = []

    init_tree = ast.parse(init_path.read_text(), filename=str(init_path))
    if _find_service_register_calls(init_tree):
        offenders.append("__init__.py calls hass.services.async_register directly")

    services_tree = ast.parse(services_path.read_text(), filename=str(services_path))
    all_register_calls = _find_service_register_calls(services_tree)
    assert all_register_calls, "expected hass.services.async_register calls in services.py"

    calls_inside_async_setup_services: list[ast.Call] = []
    for node in ast.walk(services_tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_services":
            calls_inside_async_setup_services = _find_service_register_calls(node)

    if len(calls_inside_async_setup_services) != len(all_register_calls):
        offenders.append(
            "hass.services.async_register calls exist outside async_setup_services in services.py"
        )

    assert not offenders, offenders
