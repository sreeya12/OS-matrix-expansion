"""
collect_failures.py

Phase 3, Step 1: Automated Failure Context Collection
Fetches failed workflow runs from GitHub Actions, downloads logs,
parses error patterns, and generates a draft failures CSV.

For each project in the input CSV, the script:
  1. Lists recent workflow runs on the specified branch.
  2. Identifies failed jobs and downloads their logs.
  3. Parses logs for common error patterns (stack traces, Maven errors,
     shell errors, setup-java failures).
  4. Extracts: error message, failing file path, error line, OS,
     and a best-guess failure category (Q1-Q7).
  5. Outputs a draft CSV for review before feeding to llm_os_patcher.py.

Input CSV format (same as run_baseline.py):
  project_name,workflow_file,job_name,default_branch

Usage:
    python3 collect_failures.py modified_projects.csv --token GITHUB_TOKEN
    python3 collect_failures.py modified_projects.csv --token GITHUB_TOKEN --branch os-expansion-experiment
    python3 collect_failures.py modified_projects.csv --token GITHUB_TOKEN --max-runs 5

Output:
    failures_draft.csv  -- draft failures CSV for llm_os_patcher.py
    failure_logs/       -- raw downloaded logs for manual inspection
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Error pattern definitions
# ---------------------------------------------------------------------------

# Each pattern is (regex, failure_type, description).
# The regex should have named groups where possible:
#   - 'file'    : the file path that caused the error
#   - 'line'    : the line number
#   - 'message' : the error message
ERROR_PATTERNS = [
    # Java stack trace with file:line
    (
        r"(?:at |ERROR.*?)(?P<file>[a-zA-Z][\w.]*(?:/[\w.]+)*\.java):(?P<line>\d+)",
        "Q4",
        "Java stack trace",
    ),
    # Maven surefire test failure
    # Captures: package.ClassName (stops before .methodName)
    # Handles: TestFoo (prefix), FooTest (suffix), FooTests, FooIT
    # Strategy: match lowercase.package.UppercaseClass, stop at word boundary
    (
        r"\[ERROR\]\s+(?P<file>(?:[a-z][\w]*\.)*(?:"
        r"[A-Z][\w]*(?:Test|Tests|IT)"  # suffix: FooTest, FooTests, FooIT
        r"|Test[\w]*"                    # prefix: TestFoo, TestWatchesBuilder
        r"))\b"
        r".*?(?:FAILURE|ERROR|Failed|<<<)",
        "Q4",
        "Maven test failure",
    ),
    # Maven build failure with file path (require meaningful message)
    (
        r"Failed to execute goal\s+(?P<goal>[\w.:/-]+).*?"
        r"on project (?P<project>[\w-]+):\s*"
        r"(?P<message>[A-Z].{10,200}?)(?:\s*->\s*\[Help|$)",
        "Q4",
        "Maven build failure",
    ),
    # Shell command not found / export fails on Windows
    (
        r"(?:The term '?(?P<cmd>export|bash|sudo|apt-get|apt|chmod|wget|curl|sh|ls|pwd)"
        r"'?\s*(?:is not recognized|:.*?command not found|:.*?not found)|"
        r"(?P<cmd2>\w+)\s*:\s*command not found|"
        r"is not recognized as (?:a cmdlet|an? internal|the name))",
        "Q3",
        "Shell incompatibility",
    ),
    # PowerShell specific errors
    (
        r"(?:Get-ChildItem|Set-Location|Remove-Item).*?(?:Missing an argument|"
        r"Cannot find path|not recognized)",
        "Q3",
        "PowerShell error",
    ),
    # export / bash syntax on Windows
    (
        r"export\s+\w+=.*?(?:not recognized|is not)",
        "Q3",
        "Bash export on Windows",
    ),
    # $(pwd) or $() subshell on Windows
    (
        r"\$\((?:pwd|.*?)\).*?(?:not recognized|unexpected token|invalid)",
        "Q3",
        "Bash subshell on Windows",
    ),
    # Docker not found / container action only supported on Linux
    (
        r"(?:docker:\s*command not found|"
        r"Container action is only supported on Linux|"
        r"docker:.*?invalid reference format)",
        "Q5",
        "Docker unavailable",
    ),
    # Setup Java fails
    (
        r"(?:Could not find satisfied.*?java|"
        r"(?:Set up|Setup)\s+(?:JDK|Java).*?(?:fail|error)|"
        r"actions/setup-java.*?(?:fail|error|not find)|"
        r"No.*?available for.*?(?:arm64|aarch64|macos))",
        "Q5",
        "Java setup failure",
    ),
    # UnsatisfiedLinkError (native library)
    (
        r"java\.lang\.UnsatisfiedLinkError:?\s*(?P<message>.*?)(?:\n|$)",
        "Q4",
        "Native library failure",
    ),
    # File not found with path
    (
        r"(?:No such file or directory|FileNotFoundException|"
        r"Cannot find path).*?['\"]?(?P<file>[/\\][\w./\\-]+)['\"]?",
        "Q4",
        "File not found",
    ),
    # YAML syntax error
    (
        r"(?:workflow.*?(?:syntax|parse|invalid)|"
        r"YAML.*?(?:error|invalid)|"
        r"Input does not meet YAML)",
        "Q1",
        "YAML syntax error",
    ),
    # Maven -D argument parsing on Windows
    (
        r"Unknown lifecycle phase.*?(?:\.dataFile|\.skip|\.exec)",
        "Q3",
        "Maven argument parsing on Windows",
    ),
    # Font / OS-specific resource errors
    (
        r"(?:Table '?OS/2'? does not exist|"
        r"Font.*?not (?:found|available)|"
        r"No fonts found)",
        "Q4",
        "OS-specific resource",
    ),
    # Publish unit test result action (Docker-based)
    (
        r"EnricoMi/publish-unit-test-result-action.*?(?:fail|error|Docker|container)",
        "Q5",
        "Docker-based action",
    ),
    # Generic error line with [ERROR] prefix
    # The message group starts AFTER [ERROR]\s+, so lookaheads match from there.
    # But the full match includes [ERROR], so we also filter in is_noise().
    (
        r"\[ERROR\]\s+(?P<message>(?!Tests run:)(?!There are test failures)"
        r"(?!Failed to execute goal)(?!To see the full stack)"
        r"(?!Re-run Maven)(?!For more information)"
        r"(?!\[Help)(?!->)(?!Process completed)"
        r"(?!The build could not read)"
        r"(?!\s*:\s*$)(?!\s*\"?\s*:\s)"
        r"(?!\s*$)"
        r"[^\s].{14,200})",
        "Q6",
        "Generic Maven error",
    ),
]


# ---------------------------------------------------------------------------
# Post-processing: deduplicate and clean up parsed failures
# ---------------------------------------------------------------------------

# Patterns that indicate a summary line, not a distinct failure
SUMMARY_NOISE = [
    r"^Tests run:\s*\d+",
    r"^There are test failures",
    r"^Failed to execute goal.*\[Help",
    r"^To see the full stack",
    r"^Re-run Maven",
    r"^For more information",
    r"^Process completed with exit code",
    r"^The build could not read",
    r"^See\s+.*surefire-reports",
    r"^Please refer to.*surefire-reports",
    r"^\s*:\s*$",       # empty messages like ": "
    r"^\s*$",           # blank
    r'^"\s*:\s*',       # quoted empty like '": '
    r'^\[Help\s+\d+\]', # [Help 1] links
]
SUMMARY_NOISE_RE = [re.compile(p, re.IGNORECASE) for p in SUMMARY_NOISE]


def is_noise(message):
    """Return True if the message is a Maven summary line, not a real error."""
    msg = message.strip().strip('"').strip()
    # Strip leading [ERROR] prefix if present
    if msg.startswith("[ERROR]"):
        msg = msg[7:].strip()
    # Strip GitHub Actions timestamp prefix (e.g., 2026-04-20T03:15:33.1114351Z)
    msg = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", msg)
    # Strip [ERROR] again in case it follows the timestamp
    if msg.startswith("[ERROR]"):
        msg = msg[7:].strip()
    if len(msg) < 5:
        return True
    for pat in SUMMARY_NOISE_RE:
        if pat.search(msg):
            return True
    return False


def github_api(url, token, params=None):
    """Make a GitHub API request with auth and rate limit handling."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
            wait = max(reset_time - time.time(), 60)
            print(f"  Rate limited. Waiting {int(wait)}s...", file=sys.stderr)
            time.sleep(wait + 1)
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp
    except Exception as e:
        print(f"  API error: {e}", file=sys.stderr)
        return None


def detect_os_from_job_name(job_name):
    """Guess the OS from the job name."""
    name_lower = job_name.lower()
    if "windows" in name_lower:
        return "Windows"
    elif "macos" in name_lower or "mac" in name_lower:
        return "macOS"
    elif "ubuntu" in name_lower or "linux" in name_lower:
        return "Ubuntu"
    return "unknown"


def detect_os_from_log(log_text):
    """Detect OS from log content."""
    # Check for runner OS indicators
    if "C:\\Users\\runneradmin" in log_text or "D:\\a\\" in log_text:
        return "Windows"
    elif "/Users/runner/" in log_text:
        return "macOS"
    elif "/home/runner/" in log_text:
        return "Ubuntu"
    return "unknown"


def parse_log_for_errors(log_text, project_name):
    """Parse a log for error patterns and return a list of failure entries.

    Applies noise filtering and deduplication so that summary lines
    (e.g., 'Tests run: 22, Failures: 0, Errors: 3') are removed and
    only the most specific match per test class is kept.
    """
    raw_failures = []
    seen_messages = set()

    for pattern, failure_type, description in ERROR_PATTERNS:
        for match in re.finditer(pattern, log_text, re.IGNORECASE | re.MULTILINE):
            # Extract fields from named groups
            groups = match.groupdict()
            file_path = groups.get("file", "")
            line_num = groups.get("line", "0")
            message = groups.get("message", "")

            # If no message from named group, use the whole match
            if not message:
                message = match.group(0).strip()

            # Skip noise lines
            if is_noise(message):
                continue

            # Truncate long messages
            if len(message) > 300:
                message = message[:297] + "..."

            # Deduplicate by exact match
            dedup_key = f"{failure_type}:{description}:{message[:80]}"
            if dedup_key in seen_messages:
                continue
            seen_messages.add(dedup_key)

            # Convert Java class names to file paths
            if file_path and "." in file_path and "/" not in file_path:
                # Looks like a Java class name, e.g. com.example.FooTest.methodName
                # Strip method name: find the last component that starts with uppercase
                # and contains Test/Tests/IT, then truncate there
                parts = file_path.split(".")
                class_idx = -1
                for idx_p, part in enumerate(parts):
                    if part and part[0].isupper():
                        # Could be a class name
                        if "Test" in part or "IT" in part:
                            class_idx = idx_p
                            break
                        # Keep looking; last uppercase part before a lowercase part is the class
                        class_idx = idx_p

                if class_idx >= 0:
                    class_parts = parts[:class_idx + 1]
                else:
                    class_parts = parts

                file_path_guess = (
                    "src/test/java/" + "/".join(class_parts) + ".java"
                )
            else:
                file_path_guess = file_path

            raw_failures.append({
                "file": file_path_guess,
                "line": line_num,
                "message": message,
                "failure_type": failure_type,
                "description": description,
            })

    # Deduplicate: if we have both a specific Q4 test failure and a Q6 generic
    # error for the same test class, keep only the Q4 entry.
    test_classes_with_specific = set()
    for f in raw_failures:
        if f["failure_type"] in ("Q4",) and f["description"] == "Maven test failure":
            class_match = re.search(r"([\w.]*(?:Test|Tests|IT)[\w.]*)", f["message"])
            if class_match:
                test_classes_with_specific.add(class_match.group(1))

    # Also track specific Q3/Q5 error keywords to suppress Q6 duplicates
    specific_keywords = set()
    for f in raw_failures:
        if f["failure_type"] in ("Q3", "Q5"):
            # Extract a short key phrase from the message
            words = f["message"].strip()[:60].lower()
            specific_keywords.add(words)

    # Track Q5 docker messages to suppress duplicate Q3 matches
    q5_docker_messages = set()
    for f in raw_failures:
        if f["failure_type"] == "Q5" and "docker" in f["message"].lower():
            q5_docker_messages.add(f["message"].strip().lower()[:60])

    filtered = []
    for f in raw_failures:
        # Skip Q3 entries that duplicate a Q5 docker entry
        if f["failure_type"] == "Q3" and "docker" in f["message"].lower():
            msg_lower = f["message"].strip().lower()[:60]
            if msg_lower in q5_docker_messages:
                continue

        if f["failure_type"] == "Q6" and f["description"] == "Generic Maven error":
            # Check if this generic error mentions a test class we already have
            dominated = False
            for cls in test_classes_with_specific:
                if cls in f["message"]:
                    dominated = True
                    break
            # Check if a more specific Q3/Q5 already covers this
            if not dominated:
                msg_lower = f["message"].strip()[:60].lower()
                for kw in specific_keywords:
                    # If significant overlap in first 40 chars, skip
                    if kw[:40] in msg_lower or msg_lower[:40] in kw:
                        dominated = True
                        break
            # Also check if it mentions a test class from Q4
            if not dominated:
                for cls in test_classes_with_specific:
                    # Partial match: class name appears anywhere in message
                    short_cls = cls.split(".")[-1] if "." in cls else cls
                    if short_cls in f["message"]:
                        dominated = True
                        break
            if dominated:
                continue
        filtered.append(f)

    return filtered


def get_failed_runs(owner, repo, workflow_file, branch, token, max_runs=10):
    """Fetch recent failed workflow runs."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    params = {
        "branch": branch,
        "status": "failure",
        "per_page": max_runs,
    }
    resp = github_api(url, token, params)
    if resp is None:
        return []

    data = resp.json()
    runs = data.get("workflow_runs", [])

    # Filter to the specific workflow file
    filtered = []
    for run in runs:
        if run.get("path", "").endswith(workflow_file):
            filtered.append(run)

    return filtered[:max_runs]


def get_failed_jobs(owner, repo, run_id, token):
    """Get failed jobs for a specific run."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    resp = github_api(url, token)
    if resp is None:
        return []

    data = resp.json()
    failed_jobs = []
    for job in data.get("jobs", []):
        if job.get("conclusion") == "failure":
            failed_jobs.append(job)

    return failed_jobs


def download_job_log(owner, repo, job_id, token):
    """Download the log for a specific job."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    Error downloading log for job {job_id}: {e}", file=sys.stderr)
        return None


def download_run_logs_zip(owner, repo, run_id, token, output_dir):
    """Download and extract the full logs zip for a run."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=120, allow_redirects=True)
        resp.raise_for_status()

        zip_path = os.path.join(output_dir, f"run_{run_id}.zip")
        with open(zip_path, "wb") as f:
            f.write(resp.content)

        # Extract
        extract_dir = os.path.join(output_dir, f"run_{run_id}")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            # Sometimes GitHub returns the log as plain text instead
            log_path = os.path.join(extract_dir, "log.txt")
            with open(log_path, "w") as f:
                f.write(resp.text)

        return extract_dir
    except Exception as e:
        print(f"    Error downloading logs for run {run_id}: {e}", file=sys.stderr)
        return None


def process_project(project_name, workflow_file, branch, token, log_dir, max_runs):
    """Process a single project: fetch runs, download logs, parse errors."""
    parts = project_name.split("/")
    if len(parts) != 2:
        print(f"  Invalid project: {project_name}", file=sys.stderr)
        return []

    owner, repo = parts
    print(f"\n  Fetching failed runs for {project_name} on branch '{branch}'...")

    # Get failed runs
    runs = get_failed_runs(owner, repo, workflow_file, branch, token, max_runs)
    if not runs:
        # Also try without workflow file filter
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
        params = {"branch": branch, "status": "failure", "per_page": max_runs}
        resp = github_api(url, token, params)
        if resp:
            runs = resp.json().get("workflow_runs", [])[:max_runs]

    if not runs:
        print(f"  No failed runs found.")
        return []

    print(f"  Found {len(runs)} failed run(s).")

    all_failures = []
    seen_failure_keys = set()

    # Only process the first few runs to avoid redundant data
    for run in runs[:3]:
        run_id = run["id"]
        print(f"  Processing run {run_id}...")

        # Get failed jobs
        failed_jobs = get_failed_jobs(owner, repo, run_id, token)
        if not failed_jobs:
            print(f"    No failed jobs in run {run_id}.")
            continue

        for job in failed_jobs:
            job_id = job["id"]
            job_name = job.get("name", "unknown")

            # Detect OS
            os_name = detect_os_from_job_name(job_name)

            print(f"    Job: {job_name} (OS: {os_name})")

            # Download job log
            log_text = download_job_log(owner, repo, job_id, token)
            if not log_text:
                continue

            # Detect OS from log if not found in job name
            if os_name == "unknown":
                os_name = detect_os_from_log(log_text)

            # Save log
            project_log_dir = os.path.join(log_dir, project_name.replace("/", "__"))
            os.makedirs(project_log_dir, exist_ok=True)
            safe_job_name = re.sub(r'[^\w\-.]', '_', job_name)
            log_path = os.path.join(
                project_log_dir, f"run_{run_id}_{safe_job_name}.log"
            )
            with open(log_path, "w", errors="replace") as f:
                f.write(log_text)

            # Parse errors
            failures = parse_log_for_errors(log_text, project_name)

            for failure in failures:
                # Deduplicate across runs (same error type + description)
                dedup_key = (
                    f"{project_name}:{os_name}:{failure['failure_type']}:"
                    f"{failure['description']}"
                )
                if dedup_key in seen_failure_keys:
                    continue
                seen_failure_keys.add(dedup_key)

                # Determine the failure file
                failure_file = failure["file"]
                if not failure_file:
                    # Default to workflow file for Q3/Q1 failures
                    if failure["failure_type"] in ("Q3", "Q1"):
                        failure_file = f".github/workflows/{workflow_file}"
                    else:
                        failure_file = "unknown"

                all_failures.append({
                    "project_name": project_name,
                    "workflow_file": workflow_file,
                    "failure_file": failure_file,
                    "error_line": failure["line"],
                    "error_message": failure["message"],
                    "failure_type": failure["failure_type"],
                    "target_os": os_name,
                    "branch": branch,
                    "job_name": job_name,
                    "run_id": str(run_id),
                    "description": failure["description"],
                    "log_file": log_path,
                })

        time.sleep(0.5)  # Rate limit courtesy

    return all_failures


def main():
    parser = argparse.ArgumentParser(
        description="Collect failure context from GitHub Actions logs."
    )
    parser.add_argument(
        "projects_csv",
        help="CSV with project_name, workflow_file, job_name, default_branch columns.",
    )
    parser.add_argument(
        "--token",
        help="GitHub personal access token (or set GITHUB_TOKEN env var).",
    )
    parser.add_argument(
        "--branch",
        default="os-expansion-experiment",
        help="Branch to check for failed runs (default: os-expansion-experiment).",
    )
    parser.add_argument(
        "-o", "--output",
        default="failures_draft.csv",
        help="Output CSV path (default: failures_draft.csv).",
    )
    parser.add_argument(
        "--log-dir",
        default="failure_logs",
        help="Directory to save downloaded logs (default: failure_logs/).",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=5,
        help="Maximum failed runs to fetch per project (default: 5).",
    )

    args = parser.parse_args()
    token = args.token or os.getenv("GITHUB_TOKEN")

    if not token:
        print("Error: GitHub token required. Use --token or set GITHUB_TOKEN.",
              file=sys.stderr)
        sys.exit(1)

    # Read projects CSV
    projects = []
    try:
        with open(args.projects_csv, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                projects.append(row)
    except Exception as e:
        print(f"Error reading {args.projects_csv}: {e}", file=sys.stderr)
        sys.exit(1)

    if not projects:
        print("No projects found in CSV.")
        sys.exit(0)

    print(f"Collecting failures for {len(projects)} project(s)...")
    print(f"Branch: {args.branch}")
    print(f"Output: {args.output}")
    print(f"Logs:   {args.log_dir}")

    os.makedirs(args.log_dir, exist_ok=True)

    # Process each project
    all_failures = []
    for i, row in enumerate(projects, 1):
        project_name = row.get("project_name", "")
        workflow_file = row.get("workflow_file", "")
        print(f"\n[{i}/{len(projects)}] {project_name} ({workflow_file})")

        failures = process_project(
            project_name, workflow_file, args.branch, token,
            args.log_dir, args.max_runs,
        )
        all_failures.extend(failures)
        time.sleep(0.5)

    if not all_failures:
        print("\nNo failures found across all projects.")
        sys.exit(0)

    # Write draft CSV (compatible with llm_os_patcher.py input format)
    fieldnames = [
        "project_name", "workflow_file", "failure_file", "error_line",
        "error_message", "failure_type", "target_os", "branch",
        "job_name", "run_id", "description", "log_file",
    ]
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_failures)

    print(f"\n{'=' * 60}")
    print(f"Draft failures CSV written to: {args.output}")
    print(f"Total failures found: {len(all_failures)}")
    print(f"Logs saved to: {args.log_dir}/")
    print(f"\nBreakdown by category:")
    category_counts = {}
    for f in all_failures:
        cat = f["failure_type"]
        category_counts[cat] = category_counts.get(cat, 0) + 1
    for cat in sorted(category_counts):
        print(f"  {cat}: {category_counts[cat]}")

    print(f"\nBreakdown by OS:")
    os_counts = {}
    for f in all_failures:
        os_name = f["target_os"]
        os_counts[os_name] = os_counts.get(os_name, 0) + 1
    for os_name in sorted(os_counts):
        print(f"  {os_name}: {os_counts[os_name]}")

    print(f"\nNext steps:")
    print(f"  1. Review {args.output} and correct any wrong file paths or categories.")
    print(f"  2. Remove rows you do not want to patch (e.g., Q5 dependency issues).")
    print(f"  3. Run: python3 llm_os_patcher.py {args.output} --token TOKEN --api-key KEY")


if __name__ == "__main__":
    main()