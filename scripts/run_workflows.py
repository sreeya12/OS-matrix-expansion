"""
run_baseline.py

Phase 2: Baseline Workflow Execution
Runs the original, unmodified workflows for each selected project
using GitHub Actions workflow_dispatch API.

For each project, the script:
  1. Triggers the workflow via workflow_dispatch on the default branch.
  2. Polls until the run completes (success or failure).
  3. Downloads and archives the run logs locally.
  4. Records results in a CSV: project, run_number, job_name, status,
     conclusion, duration_seconds, timestamp.

Runs are spaced 5 minutes apart to avoid GitHub rate limits.
10 runs per project by default (configurable with --runs).

Usage:
    python3 run_baseline.py my_forked_projects.csv --token YOUR_GITHUB_TOKEN
    python3 run_baseline.py my_forked_projects.csv --token YOUR_GITHUB_TOKEN --runs 3
"""

import csv
import sys
import os
import time
import argparse
import zipfile
import io
import requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration
POLL_INTERVAL = 30       # seconds between status checks
MAX_POLL_TIME = 3600     # max wait per run (1 hour)
RUN_SPACING = 300        # 5 minutes between dispatches
DEFAULT_RUNS = 10


def github_headers(token):
    """Build authorization headers for GitHub API requests."""
    return {
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': f'token {token}',
    }


def ensure_workflow_dispatch(owner, repo, workflow_file, branch, token):
    """Check if the workflow file has workflow_dispatch trigger.
    If not, add it and commit the change. Returns True if ready to dispatch."""
    import base64

    # Fetch the workflow file content
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows/{workflow_file}"
    headers = github_headers(token)
    params = {'ref': branch}

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        print(f"  Could not fetch workflow file ({resp.status_code})", file=sys.stderr)
        return False

    file_data = resp.json()
    file_sha = file_data['sha']
    content = base64.b64decode(file_data['content']).decode('utf-8')

    # Check if workflow_dispatch already exists
    if 'workflow_dispatch' in content:
        return True

    # Add workflow_dispatch to the on: trigger
    # Handle both "on: push" (short form) and "on:\n  push:" (long form)
    modified = None

    # Case 1: "on: [push, pull_request]" or "on: push"
    import re
    match = re.match(r'^(.*?\bon:\s*)(\[.+?\]|\w+)(.*?)$', content, re.MULTILINE | re.DOTALL)
    if match and 'workflow_dispatch' not in content:
        # Find the 'on:' line and convert to multi-line with workflow_dispatch
        lines = content.split('\n')
        new_lines = []
        on_found = False
        for line in lines:
            stripped = line.strip()
            if not on_found and re.match(r'^on:\s*(\[.+?\]|\w+)', stripped):
                # Single-line on: trigger, e.g., "on: push" or "on: [push, pull_request]"
                new_lines.append('on:')
                new_lines.append('  workflow_dispatch:')
                # Parse what was after on:
                after_on = re.match(r'^on:\s*(.+)$', stripped).group(1).strip()
                if after_on.startswith('[') and after_on.endswith(']'):
                    # Array form: on: [push, pull_request]
                    items = [x.strip() for x in after_on[1:-1].split(',')]
                    for item in items:
                        new_lines.append(f'  {item}:')
                else:
                    # Single trigger: on: push
                    new_lines.append(f'  {after_on}:')
                on_found = True
                continue
            elif not on_found and stripped == 'on:':
                # Multi-line on: block, just insert workflow_dispatch after it
                new_lines.append(line)
                new_lines.append('  workflow_dispatch:')
                on_found = True
                continue
            new_lines.append(line)

        if on_found:
            modified = '\n'.join(new_lines)

    if not modified:
        # Fallback: just insert workflow_dispatch: after the on: line
        lines = content.split('\n')
        new_lines = []
        for line in lines:
            new_lines.append(line)
            if line.strip() == 'on:' or re.match(r'^\s*on:\s*$', line):
                new_lines.append('  workflow_dispatch:')
        modified = '\n'.join(new_lines)

    if modified and modified != content:
        # Commit the change
        put_url = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows/{workflow_file}"
        put_body = {
            'message': 'ci: add workflow_dispatch trigger for automated runs',
            'content': base64.b64encode(modified.encode('utf-8')).decode('utf-8'),
            'sha': file_sha,
            'branch': branch,
        }
        put_resp = requests.put(put_url, headers=headers, json=put_body, timeout=15)
        if put_resp.status_code in (200, 201):
            print(f"  Added workflow_dispatch trigger to {workflow_file}")
            return True
        else:
            print(f"  Failed to add workflow_dispatch ({put_resp.status_code}): {put_resp.text[:200]}",
                  file=sys.stderr)
            return False

    return True


def trigger_workflow(owner, repo, workflow_file, branch, token):
    """Trigger a workflow_dispatch event.
    Returns True if the dispatch was accepted (HTTP 204)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches"
    payload = {'ref': branch}

    resp = requests.post(url, headers=github_headers(token), json=payload, timeout=15)
    if resp.status_code == 204:
        return True
    print(f"  Dispatch failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    return False


def get_latest_run(owner, repo, workflow_file, token, after_time=None):
    """Find the most recent workflow_dispatch run, optionally created after a given time."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
    params = {'per_page': 5, 'event': 'workflow_dispatch'}
    if after_time:
        params['created'] = f'>={after_time}'

    resp = requests.get(url, headers=github_headers(token), params=params, timeout=15)
    resp.raise_for_status()
    runs = resp.json().get('workflow_runs', [])

    if runs:
        return runs[0]
    return None


def poll_run_completion(owner, repo, run_id, token):
    """Poll a workflow run until it completes. Returns the final run object."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
    start = time.time()
    network_retries = 0
    MAX_NETWORK_RETRIES = 10

    while time.time() - start < MAX_POLL_TIME:
        try:
            resp = requests.get(url, headers=github_headers(token), timeout=15)
            resp.raise_for_status()
            network_retries = 0  # reset on success
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            network_retries += 1
            wait = min(60 * network_retries, 300)  # back off up to 5 min
            print(f"\n    Network error (attempt {network_retries}/{MAX_NETWORK_RETRIES}): {e}")
            if network_retries >= MAX_NETWORK_RETRIES:
                print(f"    Too many network failures — giving up on run {run_id}")
                return None
            print(f"    Retrying in {wait}s...")
            time.sleep(wait)
            continue

        run = resp.json()
        status = run.get('status')
        if status == 'completed':
            return run

        elapsed = int(time.time() - start)
        print(f"    Waiting... status: {status} ({elapsed}s elapsed)", end='\r', flush=True)
        time.sleep(POLL_INTERVAL)

    print(f"\n    WARNING: Run {run_id} did not complete within {MAX_POLL_TIME}s")
    return None


def get_run_jobs(owner, repo, run_id, token):
    """Fetch the jobs associated with a workflow run."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    resp = requests.get(url, headers=github_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json().get('jobs', [])


def download_run_logs(owner, repo, run_id, output_dir, token):
    """Download and extract the log archive for a workflow run."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    resp = requests.get(url, headers=github_headers(token), timeout=30, stream=True)

    if resp.status_code == 410:
        print(f"    Logs expired for run {run_id}")
        return False

    resp.raise_for_status()
    os.makedirs(output_dir, exist_ok=True)

    try:
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        z.extractall(output_dir)
        return True
    except zipfile.BadZipFile:
        raw_path = os.path.join(output_dir, 'raw_log.txt')
        with open(raw_path, 'wb') as f:
            f.write(resp.content)
        return True


def run_baseline(slug, workflow_file, branch, num_runs, token, output_base):
    """Execute a workflow num_runs times and collect baseline results."""
    owner, repo = slug.split('/')
    proj_dir = os.path.join(output_base, slug.replace('/', '__'))
    os.makedirs(proj_dir, exist_ok=True)

    # Ensure workflow_dispatch trigger exists before attempting to dispatch
    if not ensure_workflow_dispatch(owner, repo, workflow_file, branch, token):
        print(f"  ERROR: Could not ensure workflow_dispatch for {slug}/{workflow_file}")
        return [{'project': slug, 'run_number': 0, 'run_id': '',
                 'job_name': '', 'status': 'dispatch_setup_failed',
                 'conclusion': '', 'duration_seconds': 0,
                 'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}]

    results = []

    for run_num in range(1, num_runs + 1):
        print(f"\n  [{run_num}/{num_runs}] Dispatching {slug} on branch '{branch}'...")

        # Record time before dispatch to find the run afterward
        dispatch_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Trigger the workflow
        if not trigger_workflow(owner, repo, workflow_file, branch, token):
            results.append({
                'project': slug,
                'run_number': run_num,
                'run_id': '',
                'job_name': '',
                'status': 'dispatch_failed',
                'conclusion': '',
                'duration_seconds': 0,
                'timestamp': dispatch_time,
            })
            continue

        # Wait for GitHub to register the run
        time.sleep(5)

        # Find the triggered run
        run = get_latest_run(owner, repo, workflow_file, token, after_time=dispatch_time)
        if not run:
            time.sleep(10)
            run = get_latest_run(owner, repo, workflow_file, token, after_time=dispatch_time)

        if not run:
            print(f"    Could not find the triggered run")
            results.append({
                'project': slug,
                'run_number': run_num,
                'run_id': '',
                'job_name': '',
                'status': 'run_not_found',
                'conclusion': '',
                'duration_seconds': 0,
                'timestamp': dispatch_time,
            })
            continue

        run_id = run['id']
        print(f"    Run ID: {run_id}")

        # Poll until the run finishes
        final_run = poll_run_completion(owner, repo, run_id, token)
        if not final_run:
            results.append({
                'project': slug,
                'run_number': run_num,
                'run_id': run_id,
                'job_name': '',
                'status': 'timeout',
                'conclusion': '',
                'duration_seconds': MAX_POLL_TIME,
                'timestamp': dispatch_time,
            })
            continue

        conclusion = final_run.get('conclusion', 'unknown')
        created = final_run.get('created_at', '')
        updated = final_run.get('updated_at', '')

        # Calculate duration
        duration = 0
        try:
            t1 = datetime.fromisoformat(created.replace('Z', '+00:00'))
            t2 = datetime.fromisoformat(updated.replace('Z', '+00:00'))
            duration = int((t2 - t1).total_seconds())
        except Exception:
            pass

        print(f"    Conclusion: {conclusion} (duration: {duration}s)")

        # Download logs
        log_dir = os.path.join(proj_dir, f"run_{run_num:02d}")
        download_run_logs(owner, repo, run_id, log_dir, token)

        # Get per-job breakdown
        jobs = get_run_jobs(owner, repo, run_id, token)
        if jobs:
            for job in jobs:
                results.append({
                    'project': slug,
                    'run_number': run_num,
                    'run_id': run_id,
                    'job_name': job.get('name', 'unknown'),
                    'status': 'completed',
                    'conclusion': job.get('conclusion', 'unknown'),
                    'duration_seconds': duration,
                    'timestamp': dispatch_time,
                })
        else:
            results.append({
                'project': slug,
                'run_number': run_num,
                'run_id': run_id,
                'job_name': '',
                'status': 'completed',
                'conclusion': conclusion,
                'duration_seconds': duration,
                'timestamp': dispatch_time,
            })

        # Wait before next run
        if run_num < num_runs:
            print(f"    Waiting {RUN_SPACING // 60} minutes before next run...")
            time.sleep(RUN_SPACING)

    return results


def main():
    parser = argparse.ArgumentParser(
        description='Run baseline (unmodified) GitHub Actions workflows and collect results.'
    )
    parser.add_argument('input_csv',
                        help='CSV file with forked project info (needs project_name, '
                             'workflow_file, default_branch columns)')
    parser.add_argument('--token',
                        help='GitHub personal access token (or set GITHUB_TOKEN env var)')
    parser.add_argument('--runs', type=int, default=DEFAULT_RUNS,
                        help=f'Number of runs per project (default: {DEFAULT_RUNS})')
    parser.add_argument('--output', default='baseline_results',
                        help='Output directory for logs and results CSV')

    args = parser.parse_args()
    token = args.token or os.getenv('GITHUB_TOKEN')

    if not token:
        print("ERROR: GitHub token is required for workflow dispatch.")
        print("  Provide via --token or set GITHUB_TOKEN environment variable.")
        sys.exit(1)
    
    if args.token:
        print("Using GitHub token from --token argument")
    else:
        print("Using GitHub token from environment")

    # Read project list
    projects = []
    with open(args.input_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            projects.append(row)

    print(f"Baseline execution: {len(projects)} projects, {args.runs} runs each")
    print(f"Estimated time: ~{len(projects) * args.runs * 5} minutes\n")

    all_results = []

    # Open results CSV for incremental writing so progress is not lost on crash
    os.makedirs(args.output, exist_ok=True)
    results_file = os.path.join(args.output, 'baseline_results.csv')
    fieldnames = ['project', 'run_number', 'run_id', 'job_name',
                  'status', 'conclusion', 'duration_seconds', 'timestamp']

    csv_file = open(results_file, 'w', newline='', encoding='utf-8')
    csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    csv_writer.writeheader()
    csv_file.flush()

    try:
        for i, proj in enumerate(projects, 1):
            slug = proj['project_name']
            wf_file = proj['workflow_file']
            branch = proj.get('default_branch', 'main')

            print(f"\n{'=' * 60}")
            print(f"  [{i}/{len(projects)}] {slug}")
            print(f"  Workflow: {wf_file}  |  Branch: {branch}")
            print(f"{'=' * 60}")

            results = run_baseline(slug, wf_file, branch, args.runs, token, args.output)
            all_results.extend(results)

            # Write this project's rows immediately
            csv_writer.writerows(results)
            csv_file.flush()
    finally:
        csv_file.close()

    print(f"\n\nResults written to {results_file}")
    print(f"Logs saved under {args.output}/\n")

    # Summary
    total = len(all_results)
    success = sum(1 for r in all_results if r.get('conclusion') == 'success')
    failure = sum(1 for r in all_results if r.get('conclusion') == 'failure')
    other = total - success - failure
    print(f"Summary: {success} success, {failure} failure, {other} other ({total} total jobs)")


if __name__ == '__main__':
    main()