"""
select_projects.py

Phase 2, Step 1: Project Selection
Reads the CSV output from analyze_ci_workflows_v2.py and filters
for candidate projects that meet the selection criteria described
in Section 3.3 of the paper:

  1. Currently tests on 1-2 operating systems (room for expansion).
  2. Uses Maven (already filtered in Phase 1).
  3. Has commits within the past 6 months (active maintenance).
  4. Not self-hosted, no include/exclude directives (expandable).

The script outputs a shortlist CSV with project metadata and marks
each project as a candidate or not, with a reason.

Usage:
    python3 select_projects.py <analysis_csv> [github_token]

Output:
    project_candidates.csv
"""

import csv
import sys
import os
import time
import requests
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=180)


def check_recent_activity(owner, repo, token=None):
    """Check whether the repository has commits in the past 6 months.
    Returns (is_active: bool, last_commit_date: str or None).
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'

    params = {
        'per_page': 1,
        'since': SIX_MONTHS_AGO.isoformat(),
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 409:
            # Empty repository
            return False, None
        response.raise_for_status()

        commits = response.json()
        if commits and len(commits) > 0:
            date_str = commits[0]['commit']['committer']['date']
            return True, date_str
        return False, None
    except Exception as e:
        print(f"  Error checking activity for {owner}/{repo}: {e}", file=sys.stderr)
        return False, None


def get_repo_info(owner, repo, token=None):
    """Fetch basic repository metadata (stars, description, default branch)."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        return {
            'stars': data.get('stargazers_count', 0),
            'description': (data.get('description') or '')[:100],
            'default_branch': data.get('default_branch', 'main'),
            'archived': data.get('archived', False),
        }
    except Exception as e:
        print(f"  Error fetching repo info for {owner}/{repo}: {e}", file=sys.stderr)
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 select_projects.py <analysis_csv> [github_token]")
        sys.exit(1)

    input_file = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) > 2 else os.getenv('GITHUB_TOKEN')
    
    if token:
        print("Using GitHub token from environment")
    else:
        print("Warning: No GitHub token found. Rate limit is 60 requests/hour.")

    # Read the analysis CSV
    rows = []
    try:
        with open(input_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"Error reading {input_file}: {e}")
        sys.exit(1)

    print(f"Loaded {len(rows)} Maven job records from {input_file}\n")

    # Group by project: aggregate OS counts and flags
    projects = {}
    for row in rows:
        slug = row['project_name']
        if slug not in projects:
            projects[slug] = {
                'workflows': [],
                'max_os': 0,
                'min_os': 999,
                'any_self_hosted': False,
                'any_include_exclude': False,
                'any_conditionals': False,
            }

        num_os = int(row.get('num_os', len(row['current_os_config'].split(','))))
        projects[slug]['workflows'].append(row)
        projects[slug]['max_os'] = max(projects[slug]['max_os'], num_os)
        projects[slug]['min_os'] = min(projects[slug]['min_os'], num_os)

        if row.get('is_self_hosted', 'no') == 'yes':
            projects[slug]['any_self_hosted'] = True
        if row.get('has_include_exclude', 'no') == 'yes':
            projects[slug]['any_include_exclude'] = True
        if row.get('has_os_conditionals', 'no') == 'yes':
            projects[slug]['any_conditionals'] = True

    print(f"Found {len(projects)} distinct projects with Maven jobs.\n")

    # Filter and check activity
    candidates = []
    excluded = []

    for slug, info in sorted(projects.items()):
        owner, repo = slug.split('/')
        print(f"  Checking {slug}...", end=' ', flush=True)

        # Criterion 1: 1-2 OSes
        if info['max_os'] > 2:
            reason = f"already tests on {info['max_os']} OSes"
            print(f"SKIP ({reason})")
            excluded.append((slug, reason))
            continue

        # Self-hosted runners cannot be expanded
        if info['any_self_hosted']:
            reason = "uses self-hosted runners"
            print(f"SKIP ({reason})")
            excluded.append((slug, reason))
            continue

        # include/exclude directives make expansion risky
        if info['any_include_exclude']:
            reason = "matrix has include/exclude directives"
            print(f"SKIP ({reason})")
            excluded.append((slug, reason))
            continue

        # Criterion 3: recent activity
        time.sleep(0.3)
        is_active, last_commit = check_recent_activity(owner, repo, token)
        if not is_active:
            reason = "no commits in past 6 months"
            print(f"SKIP ({reason})")
            excluded.append((slug, reason))
            continue

        # Get repo info
        time.sleep(0.3)
        repo_info = get_repo_info(owner, repo, token)
        if repo_info and repo_info['archived']:
            reason = "repository is archived"
            print(f"SKIP ({reason})")
            excluded.append((slug, reason))
            continue

        print("CANDIDATE")

        # Pick the best workflow to expand (prefer single-OS jobs)
        best_workflow = None
        for wf in info['workflows']:
            wf_num_os = int(wf.get('num_os', 1))
            if best_workflow is None or wf_num_os < int(best_workflow.get('num_os', 999)):
                best_workflow = wf

        candidates.append({
            'project_name': slug,
            'workflow_file': best_workflow['workflow_file'],
            'job_name': best_workflow.get('job_name', ''),
            'current_os_config': best_workflow['current_os_config'],
            'num_os': best_workflow.get('num_os', ''),
            'has_os_conditionals': best_workflow.get('has_os_conditionals', 'no'),
            'last_commit': last_commit or '',
            'stars': repo_info['stars'] if repo_info else '',
            'description': repo_info['description'] if repo_info else '',
            'default_branch': repo_info['default_branch'] if repo_info else 'main',
        })

    # Write candidates CSV
    output_file = 'project_candidates.csv'
    fieldnames = [
        'project_name', 'workflow_file', 'job_name',
        'current_os_config', 'num_os', 'has_os_conditionals',
        'last_commit', 'stars', 'description', 'default_branch',
    ]
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(candidates)

    print(f"\n{'=' * 60}")
    print(f"  Candidates: {len(candidates)}")
    print(f"  Excluded:   {len(excluded)}")
    print(f"  Results written to {output_file}")
    print(f"{'=' * 60}\n")

    if excluded:
        print("Exclusion reasons:")
        reason_counts = {}
        for _, reason in excluded:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {count:3d}  {reason}")

    print(f"\nNext step: manually select 5-10 projects from {output_file},")
    print("fork them, and pass the shortlist to yaml_os_expander.py.")


if __name__ == '__main__':
    main()