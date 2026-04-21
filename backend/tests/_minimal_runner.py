"""
Minimal pytest-compatible runner for use in sandbox environments where
`pip install pytest` is blocked.

Supports:
- Simple `def test_*` functions
- `@pytest.mark.asyncio` async tests (auto-run with asyncio.run)
- A lightweight `monkeypatch` fixture (setattr only — enough for our suite)
- Discovering tests in a file and reporting pass/fail counts

This exists ONLY to give a green/red signal for the existing
backend/tests/test_constituents.py when pytest is unavailable. It is NOT a
replacement for pytest in a normal dev environment.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any


# ---------------------------------------------------------------------------
# Lightweight pytest shim — install into sys.modules before importing tests.
# ---------------------------------------------------------------------------

class _Marker:
    def __init__(self, name: str):
        self.name = name

    def __call__(self, fn):
        # Tag the function so the runner knows it's async
        if self.name == "asyncio":
            fn.__pytest_asyncio__ = True
        return fn


class _Mark:
    asyncio = _Marker("asyncio")

    def __getattr__(self, name: str):
        return _Marker(name)


class _PytestShim(ModuleType):
    mark = _Mark()

    @staticmethod
    def fixture(*args, **kwargs):  # noqa: D401
        def _decorator(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return _decorator


def _install_pytest_shim() -> None:
    if "pytest" in sys.modules:
        return
    shim = _PytestShim("pytest")
    shim.mark = _Mark()
    shim.fixture = _PytestShim.fixture
    sys.modules["pytest"] = shim


# ---------------------------------------------------------------------------
# monkeypatch implementation
# ---------------------------------------------------------------------------

class MonkeyPatch:
    def __init__(self):
        self._saved: list[tuple[Any, str, Any, bool]] = []

    def setattr(self, target, name_or_value, value=None, raising: bool = True):
        # Two call forms: (target, name, value) OR ("module.attr", value)
        if isinstance(target, str) and value is None and not isinstance(name_or_value, str):
            # Form: setattr("pkg.mod.attr", value)
            path, value = target, name_or_value
            mod_path, _, attr = path.rpartition(".")
            import importlib
            obj = importlib.import_module(mod_path)
            name = attr
        else:
            obj = target
            name = name_or_value
        had = hasattr(obj, name)
        old = getattr(obj, name, None)
        self._saved.append((obj, name, old, had))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old, had in reversed(self._saved):
            if had:
                setattr(obj, name, old)
            else:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
        self._saved.clear()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_test_file(path: Path) -> int:
    _install_pytest_shim()
    # Ensure project root on sys.path so `from backend.services import ...` works
    project_root = path.parent.parent.parent  # tests/ -> backend/ -> project
    sys.path.insert(0, str(project_root))

    mod = _load_module(path, path.stem)

    tests: list[tuple[str, Any]] = []
    for name, obj in inspect.getmembers(mod):
        if name.startswith("test_") and callable(obj):
            tests.append((name, obj))

    passed, failed = 0, 0
    failures: list[tuple[str, str]] = []
    for name, fn in tests:
        mp = MonkeyPatch()
        try:
            sig = inspect.signature(fn)
            kwargs = {}
            if "monkeypatch" in sig.parameters:
                kwargs["monkeypatch"] = mp

            result = fn(**kwargs)
            if inspect.iscoroutine(result):
                asyncio.run(result)
            print(f"  PASS  {name}")
            passed += 1
        except Exception:
            tb = traceback.format_exc()
            failed += 1
            failures.append((name, tb))
            print(f"  FAIL  {name}")
        finally:
            mp.undo()

    print()
    print(f"==== {passed} passed, {failed} failed ====")
    for name, tb in failures:
        print(f"\n---- FAILURE: {name} ----\n{tb}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).parent / "test_constituents.py"
    )
    sys.exit(run_test_file(target))
