#!/usr/bin/env python3
import os, subprocess, sys, tempfile
CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".yml", ".yaml")
PRODUCTION_EXTS = CODE_EXTS + (".json", ".toml", ".txt")
TEST_LIMIT = 100

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def is_test(path):
    return path.startswith("tests/") or "/tests/" in path or path.startswith("scripts/test_") or "/test_" in path

def is_code(path):
    return not is_test(path) and not path.startswith(("docs/", "scripts/ci/")) and path.endswith(CODE_EXTS)

def is_production_surface(path):
    return not is_test(path) and not path.startswith(("docs/", "scripts/ci/")) and path.endswith(PRODUCTION_EXTS)

def rows(base, head="HEAD"):
    out = []
    for line in run("git", "diff", "--numstat", base, head).splitlines():
        a, d, path = line.split("\t", 2)
        if a.isdigit() and d.isdigit(): out.append((int(a), int(d), path))
    return out

def collected_test_count(cwd=None):
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return sum("::" in line for line in result.stdout.splitlines())

def collected_test_count_at(ref):
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "base")
        subprocess.check_call(["git", "worktree", "add", "--detach", "--quiet", path, ref])
        try:
            return collected_test_count(path)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", path], capture_output=True)

base = os.getenv("BASE_SHA") or "HEAD^"
diff = rows(base)
messages = run("git", "log", "--format=%B", f"{base}..HEAD")
code_add = sum(a for a, _, p in diff if is_code(p))
code_files = {p for _, _, p in diff if is_code(p)}
production_files = {p for _, _, p in diff if is_production_surface(p)}
production_frozen = "Production-Frozen: true" in messages
failed = False

if production_frozen and production_files:
    print("PRODUCTION FREEZE FAIL: package declares Production-Frozen: true but changes production surface:")
    for path in sorted(production_files):
        print(f"  - {path}")
    failed = True

if (code_add > 50 or len(code_files) > 3) and "Jim-Approved-Budget:" not in messages:
    print(f"BUDGET FAIL: production additions={code_add}, files={len(code_files)}")
    failed = True

test_count = collected_test_count()
base_test_count = collected_test_count_at(base)
if max(base_test_count, test_count) > TEST_LIMIT and test_count > base_test_count:
    print(f"TEST RATCHET FAIL: candidate tests={test_count}, base tests={base_test_count}; no package may increase collected tests while above {TEST_LIMIT}")
    failed = True
if max(base_test_count, test_count) <= TEST_LIMIT and test_count > base_test_count and "Production-Critical-Test:" not in messages:
    print("TEST JUSTIFICATION FAIL: added tests require Production-Critical-Test: <reason>")
    failed = True
print(
    f"development_guard: code=+{code_add}/{len(code_files)} files "
    f"production_surface={len(production_files)} frozen={str(production_frozen).lower()} "
    f"tests={test_count} base_tests={base_test_count}"
)
raise SystemExit(1 if failed else 0)
