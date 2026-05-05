"""
patch_workflows.py

Diagnoses and fixes workflow_dispatch failures:
  1. Validates the token has 'workflow' scope.
  2. For each project in the CSV, fetches the workflow file from GitHub
     and adds 'workflow_dispatch:' if it is missing.
  3. Uses the Git Data API (blobs/trees/commits) instead of the Contents
     API so that fork files inherited from upstream can be updated.

Usage:
    python3 patch_workflows.py my_forked_projects.csv
    python3 patch_workflows.py my_forked_projects.csv --dry-run
"""

import csv
import sys
import os
import base64
import argparse
import re
import requests

# Load .env from the same directory as this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(_script_dir, ".env"))
except ImportError:
    pass


def _h(token):
    return {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }


def _get(url, token, **kwargs):
    resp = requests.get(url, headers=_h(token), timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _post(url, token, payload):
    resp = requests.post(url, headers=_h(token), json=payload, timeout=15)
    if not resp.ok:
        print(f"    POST {url} -> {resp.status_code}: {resp.text[:400]}")
        resp.raise_for_status()
    return resp.json()


def _patch(url, token, payload):
    resp = requests.patch(url, headers=_h(token), json=payload, timeout=15)
    if not resp.ok:
        print(f"    PATCH {url} -> {resp.status_code}: {resp.text[:400]}")
        resp.raise_for_status()
    return resp.json()


# ─── Token check ──────────────────────────────────────────────────────────────

def check_token_scopes(token):
    resp = requests.get("https://api.github.com/user", headers=_h(token), timeout=10)
    resp.raise_for_status()
    login = resp.json().get("login")
    scopes = [s.strip() for s in resp.headers.get("X-OAuth-Scopes", "").split(",") if s.strip()]
    print(f"  Authenticated as: {login}")
    print(f"  Token scopes: {scopes or '(none — likely a fine-grained PAT)'}")

    is_fine_grained = not scopes
    if is_fine_grained:
        print("  Fine-grained PAT detected.")
        print("  Required permissions: Contents (Read & Write) + Actions (Read & Write).")
        print("  Also ensure the PAT has access to ALL forked repos (not just selected ones).")
    else:
        if "workflow" not in scopes:
            print("  WARNING: missing 'workflow' scope — workflow_dispatch will be rejected by GitHub.")
        if "repo" not in scopes and "public_repo" not in scopes:
            print("  WARNING: missing 'repo'/'public_repo' scope — write operations will fail with 404.")
        if "repo" in scopes or "public_repo" in scopes:
            print("  Token has sufficient repo scope.")
        if "workflow" in scopes:
            print("  Token has 'workflow' scope.")

    return login, scopes


def check_repo_permissions(owner, repo, token):
    """Return the permissions dict for the repo, or None on failure."""
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers=_h(token), timeout=10,
    )
    if not resp.ok:
        return None
    data = resp.json()
    perms = data.get("permissions", {})
    return perms


# ─── Workflow YAML patching ────────────────────────────────────────────────────

def add_workflow_dispatch(content):
    """Insert 'workflow_dispatch:' into the 'on:' block. Returns (new_content, changed)."""
    if "workflow_dispatch" in content:
        return content, False

    # Case 1: compact  on: [push, pull_request]
    m = re.search(r'^(on:\s*)\[(.+)\]', content, re.MULTILINE)
    if m:
        items = [t.strip() for t in m.group(2).split(",")]
        block_items = "\n".join(f"  {item}:" for item in items)
        replacement = f"on:\n  workflow_dispatch:\n{block_items}"
        return content[:m.start()] + replacement + content[m.end():], True

    # Case 2: block  on:\n  push:
    m = re.search(r'^(on:\s*\n)', content, re.MULTILINE)
    if m:
        return content[:m.end()] + "  workflow_dispatch:\n" + content[m.end():], True

    return content, False


# ─── Git Data API helpers ──────────────────────────────────────────────────────

def get_branch_sha(owner, repo, branch, token):
    """Return (commit_sha, tree_sha) for the tip of branch."""
    data = _get(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
        token,
    )
    commit_sha = data["object"]["sha"]
    commit = _get(
        f"https://api.github.com/repos/{owner}/{repo}/git/commits/{commit_sha}",
        token,
    )
    return commit_sha, commit["tree"]["sha"]


def get_blob_content(owner, repo, tree_sha, file_path, token):
    """Walk the tree to find the blob for file_path and return its (content, sha).
    file_path is relative to repo root, e.g. '.github/workflows/maven.yml'.
    """
    parts = file_path.lstrip("/").split("/")
    current_tree_sha = tree_sha

    for i, part in enumerate(parts):
        tree = _get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{current_tree_sha}",
            token,
        )
        matched = next((e for e in tree["tree"] if e["path"] == part), None)
        if not matched:
            return None, None
        if i == len(parts) - 1:
            # This should be the blob
            blob = _get(
                f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{matched['sha']}",
                token,
            )
            encoding = blob.get("encoding", "base64")
            if encoding == "base64":
                content = base64.b64decode(blob["content"]).decode("utf-8")
            else:
                content = blob["content"]
            return content, matched["sha"]
        else:
            current_tree_sha = matched["sha"]

    return None, None


def commit_file(owner, repo, branch, file_path, new_content, base_tree_sha, parent_commit_sha, message, token):
    """Create a blob → tree → commit → update ref for a single file change."""
    # 1. Create blob
    blob = _post(
        f"https://api.github.com/repos/{owner}/{repo}/git/blobs",
        token,
        {"content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
    )

    # 2. Create tree with that blob
    tree = _post(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees",
        token,
        {
            "base_tree": base_tree_sha,
            "tree": [{"path": file_path.lstrip("/"), "mode": "100644", "type": "blob", "sha": blob["sha"]}],
        },
    )

    # 3. Create commit
    commit = _post(
        f"https://api.github.com/repos/{owner}/{repo}/git/commits",
        token,
        {"message": message, "tree": tree["sha"], "parents": [parent_commit_sha]},
    )

    # 4. Update branch ref
    _patch(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
        token,
        {"sha": commit["sha"]},
    )

    return commit["sha"]


# ─── Per-project logic ────────────────────────────────────────────────────────

def process_project(slug, workflow_file, branch, token, dry_run=False):
    owner, repo = slug.split("/")
    file_path = f".github/workflows/{workflow_file}"

    print(f"\n{'─'*60}")
    print(f"  {slug}  |  {workflow_file}  |  branch: {branch}")

    # Check write permissions before attempting any writes
    perms = check_repo_permissions(owner, repo, token)
    if perms is None:
        print(f"  ERROR: Could not access repo '{slug}'. Check token and repo name.")
        return "error"
    can_push = perms.get("push", False)
    can_admin = perms.get("admin", False)
    print(f"  Repo permissions — push: {can_push}, admin: {can_admin}")
    if not can_push and not dry_run:
        print(f"  ERROR: Token has no push access to '{slug}'.")
        print(f"         Fix: regenerate your PAT with 'repo' + 'workflow' scopes (classic),")
        print(f"              or for fine-grained PAT add Contents:Write + Actions:Write for this repo.")
        return "no_push_access"

    # Get branch tip
    try:
        commit_sha, tree_sha = get_branch_sha(owner, repo, branch, token)
    except Exception as e:
        print(f"  ERROR getting branch '{branch}': {e}")
        return "error"

    print(f"  Branch tip commit: {commit_sha[:12]}")

    # Walk the tree to find the workflow file
    content, blob_sha = get_blob_content(owner, repo, tree_sha, file_path, token)
    if content is None:
        print(f"  ERROR: '{file_path}' not found in tree.")
        # List what IS in .github/workflows/
        try:
            wf_dir = _get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{tree_sha}",
                token,
            )
            github_tree = next(
                (e for e in wf_dir["tree"] if e["path"] == ".github"), None
            )
            if github_tree:
                sub = _get(
                    f"https://api.github.com/repos/{owner}/{repo}/git/trees/{github_tree['sha']}",
                    token,
                )
                wf_entry = next((e for e in sub["tree"] if e["path"] == "workflows"), None)
                if wf_entry:
                    wf_tree = _get(
                        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{wf_entry['sha']}",
                        token,
                    )
                    print(f"  Available workflow files: {[e['path'] for e in wf_tree['tree']]}")
        except Exception:
            pass
        return "not_found"

    if "workflow_dispatch" in content:
        print(f"  OK: workflow_dispatch already present (blob {blob_sha[:12]}).")
        return "already_present"

    new_content, changed = add_workflow_dispatch(content)
    if not changed:
        print(f"  WARNING: Could not inject workflow_dispatch (unusual 'on:' format).")
        return "inject_failed"

    if dry_run:
        print(f"  DRY RUN: Would patch {file_path}")
        # Show first changed line
        for old, new in zip(content.splitlines(), new_content.splitlines()):
            if old != new:
                print(f"    - {old}")
                print(f"    + {new}")
        return "dry_run"

    commit = commit_file(
        owner, repo, branch, file_path,
        new_content, tree_sha, commit_sha,
        "ci: add workflow_dispatch trigger for baseline testing",
        token,
    )
    print(f"  PATCHED: committed {commit[:12]} to {branch}.")
    return "patched"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Add workflow_dispatch to forked CI workflows.")
    parser.add_argument("input_csv", help="CSV with project_name, workflow_file, default_branch columns")
    parser.add_argument("--token", help="GitHub PAT (or set GITHUB_TOKEN in .env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without pushing")
    args = parser.parse_args()

    token = args.token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: GitHub token not found.")
        print("  Add GITHUB_TOKEN=ghp_... to your .env file, or pass --token.")
        sys.exit(1)

    print("=== Token check ===")
    check_token_scopes(token)

    projects = []
    with open(args.input_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("project_name"):
                projects.append(row)

    print(f"\n=== Processing {len(projects)} project(s) ===")
    counts: dict[str, int] = {}
    for proj in projects:
        result = process_project(
            proj["project_name"],
            proj["workflow_file"],
            proj.get("default_branch", "main"),
            token,
            dry_run=args.dry_run,
        )
        counts[result] = counts.get(result, 0) + 1

    print(f"\n{'='*60}")
    print("Summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    if counts.get("patched", 0) > 0 and not args.dry_run:
        print("\nWorkflows patched. Wait ~30s for GitHub to index them,")
        print("then re-run: python3 run_workflows.py my_forked_projects.csv --runs 3")


if __name__ == "__main__":
    main()
