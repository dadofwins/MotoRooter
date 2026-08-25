"""`ErrorCode` has to sit below both the API and the domain models.

`api/error_codes.py` imports `trips.errors` to build `ERROR_TABLE`. Once `trips.models`
needs to reference a code — `TripLeg.last_routing_error` — importing it back from the API
layer would invert the dependency and be one import away from a cycle. So the enum lives on
its own at the root, importing nothing from the package.

This is a layering test rather than a behaviour test: it fails when someone adds a
convenient import to the enum module, which is exactly when the inversion creeps back.
"""

import ast
from pathlib import Path

from motorooter.error_codes import ErrorCode

MODULE = Path(__file__).resolve().parents[1] / "src" / "motorooter" / "error_codes.py"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_enum_module_imports_nothing_from_motorooter():
    """Anything it imports, `trips.models` inherits — and that is how a cycle starts."""
    offenders = {name for name in _imported_modules(MODULE) if name.startswith("motorooter")}
    assert not offenders, f"error_codes.py must stay dependency-free, but imports {offenders}"


def test_the_api_layer_still_exposes_it():
    """Existing imports keep working, and schemas.py generates the union from there."""
    from motorooter.api.error_codes import ErrorCode as ReExported

    assert ReExported is ErrorCode


def test_the_domain_models_can_reference_it_without_importing_the_api():
    from motorooter.trips import models

    assert "motorooter.api" not in str(_imported_modules(Path(models.__file__)))
