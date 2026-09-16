from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_MODULES = ("lifeos.jobs", "lifeos.mail", "lifeos.newsletter")


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    imported: str


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def imports_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def violations(root: Path, forbidden_prefixes: tuple[str, ...]) -> list[ImportViolation]:
    found: list[ImportViolation] = []
    for path in python_files(root):
        for module in imported_modules(path):
            if imports_prefix(module, forbidden_prefixes):
                try:
                    display_path = path.relative_to(REPO_ROOT)
                except ValueError:
                    display_path = path.relative_to(root)
                found.append(ImportViolation(display_path, module))
    return found


def write_source(root: Path, relative_path: str, source: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


class ArchitectureGuardTests(unittest.TestCase):
    def test_core_does_not_import_product_domains(self) -> None:
        self.assertEqual(violations(REPO_ROOT / "lifeos" / "core", DOMAIN_MODULES), [])

    def test_integrations_do_not_import_jobs_business_policy(self) -> None:
        self.assertEqual(violations(REPO_ROOT / "lifeos" / "integrations", ("lifeos.jobs",)), [])

    def test_security_and_performance_harnesses_stay_domain_neutral(self) -> None:
        checked_roots = (
            REPO_ROOT / "scripts" / "security",
            REPO_ROOT / "tests" / "security",
            REPO_ROOT / "tests" / "performance",
        )
        found = [
            violation
            for root in checked_roots
            for violation in violations(root, DOMAIN_MODULES)
        ]

        self.assertEqual(found, [])

    def test_synthetic_core_to_domain_import_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_source(root, "runtime.py", "from lifeos.jobs.identity import stable_job_key\n")

            found = violations(root, DOMAIN_MODULES)

        self.assertEqual([violation.imported for violation in found], ["lifeos.jobs.identity"])

    def test_synthetic_shared_harness_to_domain_import_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_source(root, "budget.py", "import lifeos.newsletter.processor\n")

            found = violations(root, DOMAIN_MODULES)

        self.assertEqual([violation.imported for violation in found], ["lifeos.newsletter.processor"])

    def test_synthetic_allowed_dependencies_pass(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_source(
                root,
                "http.py",
                "\n".join(
                    [
                        "from lifeos.core.runtime import RunContext",
                        "from lifeos.integrations.notion import NotionTransport",
                        "import pathlib",
                    ]
                ),
            )

            found = violations(root, DOMAIN_MODULES)

        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
