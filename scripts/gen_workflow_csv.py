#!/usr/bin/env python3
"""
Generate a workflow CSV for run_workflows.py from a GitHub repository URL.
Automatically selects the primary CI/build workflow using scoring heuristics.

Usage:
    python gen_workflow_csv.py https://github.com/sreeya12/java-design-patterns
    python gen_workflow_csv.py sreeya12/jedis
    python gen_workflow_csv.py sreeya12/jedis -o jedis.csv
    python gen_workflow_csv.py sreeya12/jedis --all   # include all Maven workflows
"""

import argparse
import os
import sys
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()


def parse_repo(repo_input):
    repo_input = repo_input.strip().rstrip("/")
    if "github.com" in repo_input:
        parts = repo_input.split("github.com/")[-1].split("/")
        return f"{parts[0]}/{parts[1]}"
    return repo_input


def get_default_branch(slug, headers):
    r = requests.get(f"https://api.github.com/repos/{slug}", headers=headers)
    r.raise_for_status()
    return r.json().get("default_branch", "main")


def get_workflow_files(slug, headers):
    url = f"https://api.github.com/repos/{slug}/contents/.github/workflows"
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [f for f in r.json() if f["name"].endswith((".yml", ".yaml"))]


def fetch_yaml_content(download_url, headers):
    r = requests.get(download_url, headers=headers)
    r.raise_for_status()
    return yaml.safe_load(r.text)


def is_maven_job(job_def):
    for step in job_def.get("steps", []):
        run_cmd = step.get("run", "")
        uses_cmd = step.get("uses", "")
        name = step.get("name", "")
        if "mvn" in run_cmd or "setup-java" in uses_cmd or "maven" in name.lower():
            return True
    return False


def has_build_or_test(job_def):
    for step in job_def.get("steps", []):
        run_cmd = step.get("run", "").lower()
        name = step.get("name", "").lower()
        keywords = ["mvn", "test", "build", "compile", "install", "verify"]
        if any(kw in run_cmd or kw in name for kw in keywords):
            return True
    return False


def score_workflow(filename, content):
    """Score how likely this is the primary CI workflow. Higher = more likely."""
    score = 0
    name_lower = filename.lower().replace(".yml", "").replace(".yaml", "")
    display_name = (content.get("name", "") or "").lower()

    # Positive: filename matches common CI names
    exact_matches = ["ci", "build", "maven", "test", "unit-test", "unit-tests",
                     "main", "maven-ci", "java-ci"]
    for m in exact_matches:
        if name_lower == m:
            score += 50
        elif m in name_lower:
            score += 30

    # Positive: display name matches
    for kw in ["ci", "build", "maven", "java ci", "unit test"]:
        if kw in display_name:
            score += 20

    # Positive: triggers on push (primary CI usually does)
    triggers = content.get("on", content.get(True, {}))
    if isinstance(triggers, dict):
        if "push" in triggers:
            score += 15
        if "pull_request" in triggers:
            score += 10
        if "workflow_call" in triggers and len(triggers) == 1:
            score -= 10
    elif isinstance(triggers, list):
        if "push" in triggers:
            score += 15
        if "pull_request" in triggers:
            score += 10

    # Positive: has Maven jobs that actually build/test
    for job_name, job_def in content.get("jobs", {}).items():
        if isinstance(job_def, dict) and is_maven_job(job_def) and has_build_or_test(job_def):
            score += 10

    # Negative: filename suggests non-CI purpose
    negative = [
        "snapshot", "release", "deploy", "publish", "nightly",
        "format", "lint", "style", "checkstyle", "spotless",
        "doctest", "doc", "javadoc",
        "integration", "benchmark", "perf",
        "coverage", "sonar", "codecov",
        "docker", "container",
        "stale", "label", "greeting", "welcome",
        "dependabot", "renovate",
        "codeql", "security", "scorecard",
        "funding", "sponsor",
        "pr-builder", "pr_builder", "presubmit",
    ]
    for pat in negative:
        if pat in name_lower:
            score -= 40
        if pat in display_name:
            score -= 30

    return score


def extract_maven_jobs(content):
    jobs = content.get("jobs", {})
    return [name for name, defn in jobs.items()
            if isinstance(defn, dict) and is_maven_job(defn)]


def main():
    parser = argparse.ArgumentParser(
        description="Generate workflow CSV from a GitHub repo URL")
    parser.add_argument("repo", help="GitHub repo URL or owner/repo slug")
    parser.add_argument("-o", "--output", default="workflows.csv",
                        help="Output CSV file (default: workflows.csv)")
    parser.add_argument("--all", action="store_true",
                        help="Include all Maven workflows, not just the top one")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
        print("Using GitHub token.")
    else:
        print("Warning: No GITHUB_TOKEN. Rate limits will be strict.")

    slug = parse_repo(args.repo)
    print(f"\nAnalyzing {slug}...")

    default_branch = get_default_branch(slug, headers)
    print(f"Default branch: {default_branch}")

    workflow_files = get_workflow_files(slug, headers)
    if not workflow_files:
        print("No workflow files found.")
        sys.exit(1)
    print(f"Found {len(workflow_files)} workflow file(s).\n")

    scored = []
    for wf in workflow_files:
        filename = wf["name"]
        print(f"  {filename}...", end=" ")
        try:
            content = fetch_yaml_content(wf["download_url"], headers)
            if content is None:
                print("empty.")
                continue
            maven_jobs = extract_maven_jobs(content)
            if not maven_jobs:
                print("no Maven jobs.")
                continue
            sc = score_workflow(filename, content)
            print(f"score={sc}, jobs={maven_jobs}")
            scored.append((sc, filename, maven_jobs))
        except Exception as e:
            print(f"error: {e}")

    if not scored:
        print("\nNo Maven workflows found.")
        sys.exit(1)

    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"\nRanked:")
    for i, (sc, fn, jobs) in enumerate(scored):
        tag = " <-- selected" if (not args.all and i == 0) else ""
        print(f"  {sc:>4}  {fn} ({', '.join(jobs)}){tag}")

    if args.all:
        selected = [(fn, jobs) for sc, fn, jobs in scored if sc > -10]
    else:
        selected = [(scored[0][1], scored[0][2])]

    rows = []
    for filename, maven_jobs in selected:
        for job_name in maven_jobs:
            rows.append(f"{slug},{filename},{job_name},{default_branch}")

    with open(args.output, "w") as f:
        f.write("project_name,workflow_file,job_name,default_branch\n")
        for row in rows:
            f.write(row + "\n")

    print(f"\nWritten to {args.output}:")
    print("project_name,workflow_file,job_name,default_branch")
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()