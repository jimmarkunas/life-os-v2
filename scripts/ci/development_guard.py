#!/usr/bin/env python3
import os, subprocess, sys, tempfile
CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".yml", ".yaml")
TEST_LIMIT = 100

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def is_test(path):
    return path.startswith("tests/") or "/tests/" in path or path.startswith("scripts/test_") or "/test_" in path

def is_code(path):
    return not is_test(path) and not path.startswith(("docs/", "scripts/ci/")) and path.endswith(CODE_EXTS)

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
failed = False
if (code_add > 50 or len(code_files) > 3) and "Jim-Approved-Budget:" not in messages:
    print(f"BUDGET FAIL: production additions={code_add}, files={len(code_files)}")
    failed = True
test_count = collected_test_count()
base_test_count = collected_test_count_at(base)
if max(base_test_count, test_count) > TEST_LIMIT and code_files and test_count >= base_test_count:
    print(f"TEST RATCHET FAIL: candidate tests={test_count}, base tests={base_test_count}; production code changes must reduce collected tests")
    failed = True
if max(base_test_count, test_count) <= TEST_LIMIT and test_count > base_test_count and "Production-Critical-Test:" not in messages:
    print("TEST JUSTIFICATION FAIL: added tests require Production-Critical-Test: <reason>")
    failed = True
print(f"development_guard: code=+{code_add}/{len(code_files)} files tests={test_count} base_tests={base_test_count}")
raise SystemExit(1 if failed else 0)
