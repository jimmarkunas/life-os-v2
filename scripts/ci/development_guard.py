#!/usr/bin/env python3
import os, subprocess, sys
CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".yml", ".yaml")
TEST_LIMIT = 100

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def is_test(path):
    return path.startswith("tests/") or "/tests/" in path or path.startswith("scripts/test_") or "/test_" in path

def is_code(path):
    return not is_test(path) and not path.startswith("docs/") and path.endswith(CODE_EXTS)

def rows(base, head="HEAD"):
    out = []
    for line in run("git", "diff", "--numstat", base, head).splitlines():
        a, d, path = line.split("\t", 2)
        if a.isdigit() and d.isdigit(): out.append((int(a), int(d), path))
    return out

base = os.getenv("BASE_SHA") or "HEAD^"
diff = rows(base)
messages = run("git", "log", "--format=%B", f"{base}..HEAD")
code_add = sum(a for a, _, p in diff if is_code(p))
code_files = {p for _, _, p in diff if is_code(p)}
test_add = sum(a for a, _, p in diff if is_test(p))
test_del = sum(d for _, d, p in diff if is_test(p))
failed = False
if (code_add > 50 or len(code_files) > 3) and "Jim-Approved-Budget:" not in messages:
    print(f"BUDGET FAIL: production additions={code_add}, files={len(code_files)}")
    failed = True
result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], capture_output=True, text=True)
test_count = sum("::" in line for line in result.stdout.splitlines())
if test_count > TEST_LIMIT and code_files and (test_add > 0 or test_del == 0):
    print(f"TEST RATCHET FAIL: {test_count}>{TEST_LIMIT}; code changes must delete tests and add none")
    failed = True
if test_count <= TEST_LIMIT and test_add > 0 and "Production-Critical-Test:" not in messages:
    print("TEST JUSTIFICATION FAIL: added tests require Production-Critical-Test: <reason>")
    failed = True
print(f"development_guard: code=+{code_add}/{len(code_files)} files tests={test_count} test_lines=+{test_add}/-{test_del}")
raise SystemExit(1 if failed else 0)
