#!/usr/bin/env python3
import os, re, subprocess, sys

CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".yml", ".yaml"}
TEST_LIMIT = 100


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def is_test(path):
    return path.startswith("tests/") or "/tests/" in path or path.startswith("scripts/test_") or "/test_" in path


def is_code(path):
    return not is_test(path) and not path.startswith("docs/") and any(path.endswith(ext) for ext in CODE_EXTS)


def numstat(base, head):
    rows = []
    for line in run("git", "diff", "--numstat", base, head).splitlines():
        a, d, path = line.split("\t", 2)
        if a.isdigit() and d.isdigit():
            rows.append((int(a), int(d), path))
    return rows


base = os.getenv("BASE_SHA") or "HEAD^"
commits = run("git", "rev-list", "--reverse", f"{base}..HEAD").splitlines() or ["HEAD"]
failed = False
for commit in commits:
    rows = numstat(f"{commit}^", commit)
    added = sum(a for a, _, p in rows if is_code(p))
    files = {p for _, _, p in rows if is_code(p)}
    msg = run("git", "show", "-s", "--format=%B", commit)
    if (added > 50 or len(files) > 3) and "Jim-Approved-Budget:" not in msg:
        print(f"BUDGET FAIL {commit[:8]}: production additions={added}, files={len(files)}")
        failed = True
rows = numstat(base, "HEAD")
test_add = sum(a for a, _, p in rows if is_test(p))
test_del = sum(d for _, d, p in rows if is_test(p))
prod_changed = any(is_code(p) for _, _, p in rows)
collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], capture_output=True, text=True)
test_count = sum("::" in line for line in collected.stdout.splitlines())
messages = run("git", "log", "--format=%B", f"{base}..HEAD")
if test_count > TEST_LIMIT and prod_changed and (test_add > 0 or test_del == 0):
    print(f"TEST RATCHET FAIL: {test_count}>{TEST_LIMIT}; code changes must delete tests and add none")
    failed = True
if test_count <= TEST_LIMIT and test_add > 0 and "Production-Critical-Test:" not in messages:
    print("TEST JUSTIFICATION FAIL: added tests require Production-Critical-Test: <reason>")
    failed = True
print(f"development_guard: tests={test_count} test_lines=+{test_add}/-{test_del}")
raise SystemExit(1 if failed else 0)
