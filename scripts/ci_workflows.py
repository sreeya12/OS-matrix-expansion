"""
analyze_ci_workflows_v2.py

Phase 1: Dataset Characterization
Fetches GitHub Actions workflow YAMLs from a list of Java repos,
identifies Maven jobs, and extracts the concrete OS configuration
for each job. Unlike v1, this version resolves matrix variable
references (e.g. ${{ matrix.os }}) in runs-on to the actual OS
names declared in strategy.matrix.os.

Usage:
    python3 analyze_ci_workflows_v2.py <input_csv> [github_token]

Input CSV must contain a 'slug' column with owner/repo values.
"""

import yaml
import requests
import csv
import sys
import time
import os
import re
from collections import Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Known GitHub-hosted runner labels that map to a recognizable OS family.
# This list covers the labels documented in GitHub Actions as of 2025.
KNOWN_OS_LABELS = {
    'ubuntu-latest', 'ubuntu-24.04', 'ubuntu-22.04', 'ubuntu-20.04',
    'windows-latest', 'windows-2022', 'windows-2019',
    'macos-latest', 'macos-15', 'macos-14', 'macos-13', 'macos-12',
}


def fetch_workflow_files(owner, repo, token=None):
    """Retrieve the list of YAML workflow files from .github/workflows."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows"
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 404:
            return []
        response.raise_for_status()

        files = response.json()
        workflow_files = []
        for file in files:
            if file['name'].endswith(('.yml', '.yaml')):
                workflow_files.append({
                    'name': file['name'],
                    'download_url': file['download_url']
                })
        return workflow_files
    except Exception as e:
        print(f"  Error fetching workflows for {owner}/{repo}: {e}", file=sys.stderr)
        return []


def download_workflow_content(download_url):
    """Download the raw content of a single workflow file."""
    try:
        response = requests.get(download_url, timeout=10)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"  Error downloading workflow: {e}", file=sys.stderr)
        return None


def is_maven_job(job_config):
    """Return True if any step in the job references Maven."""
    if not isinstance(job_config, dict):
        return False

    steps = job_config.get('steps', [])
    for step in steps:
        if not isinstance(step, dict):
            continue

        # Check run commands for 'mvn' or 'mvnw'
        run_cmd = step.get('run', '')
        if 'mvn' in str(run_cmd).lower():
            return True

        # Check uses for maven-related actions
        uses = step.get('uses', '')
        if 'maven' in str(uses).lower():
            return True

        # Check step name
        name = step.get('name', '')
        if 'maven' in str(name).lower():
            return True

    return False


def _is_matrix_reference(value):
    """Check whether a string is a GitHub Actions expression referencing
    a matrix variable, e.g. '${{ matrix.os }}' or '${{ matrix.operating-system }}'.
    """
    if not isinstance(value, str):
        return False
    return bool(re.search(r'\$\{\{\s*matrix\.', value))


def _extract_matrix_var_name(value):
    """Extract the variable name from an expression like '${{ matrix.os }}'.
    Returns the name (e.g. 'os') or None.
    """
    m = re.search(r'\$\{\{\s*matrix\.(\S+?)\s*\}\}', value)
    if m:
        return m.group(1)
    return None


def _normalize_os(label):
    """Return a canonical family name for a runner label, or the label
    itself if it is not recognized. This helps deduplicate entries like
    'ubuntu-22.04' and 'ubuntu-latest' when they appear together.
    """
    label = label.strip().lower()
    if label.startswith('ubuntu'):
        return label  # keep full label for accuracy
    if label.startswith('windows'):
        return label
    if label.startswith('macos'):
        return label
    return label


def _extract_os_from_includes(matrix, var_name=None):
    """Extract OS values from matrix 'include' entries.

    Many workflows define OS values only inside 'include' blocks
    without a top-level 'os' array.  For example:
        strategy:
          matrix:
            include:
              - setup: linux-x86_64
                os: ubuntu-latest
              - setup: windows-x86_64
                os: windows-2022

    We look for the key matching var_name (e.g. 'os') inside each
    include entry and collect the distinct values.
    """
    includes = matrix.get('include', [])
    if not isinstance(includes, list):
        return []

    # Determine which key to look for in include entries.
    # Default to 'os' if no specific variable name was provided.
    keys_to_check = []
    if var_name:
        keys_to_check.append(var_name)
    if 'os' not in keys_to_check:
        keys_to_check.append('os')
    keys_to_check.extend(['operating-system', 'platform'])

    os_values = []
    for entry in includes:
        if not isinstance(entry, dict):
            continue
        for key in keys_to_check:
            if key in entry:
                val = str(entry[key]).strip()
                if val and val not in os_values:
                    os_values.append(val)
                break  # only take one OS key per entry

    return os_values


def _extract_fallback_from_expression(expr):
    """Extract a fallback value from GitHub Actions expressions like:
        ${{ vars.ubuntu_small || 'ubuntu-latest' }}
        ${{ github.event.inputs.os || 'ubuntu-latest' }}

    Returns the fallback string (e.g. 'ubuntu-latest') or None.
    """
    if not isinstance(expr, str):
        return None
    # Match patterns like: || 'value'  or  || "value"
    m = re.search(r"\|\|\s*['\"]([^'\"]+)['\"]", expr)
    if m:
        return m.group(1)
    return None


def resolve_os_list(job_config):
    """Determine the concrete set of operating systems a job will run on.

    The function handles five cases:
      1. runs-on is a plain string such as 'ubuntu-latest'.
      2. runs-on references a matrix variable (e.g. '${{ matrix.os }}')
         and the matrix definition provides a top-level list of values.
      3. runs-on references a matrix variable whose values are defined
         only inside 'include' entries (no top-level 'os' array).
      4. The matrix has an 'os' (or similarly named) key with a list
         of OS labels, regardless of what runs-on says.
      5. runs-on uses a vars/inputs expression with a fallback value
         (e.g. '${{ vars.ubuntu_small || 'ubuntu-latest' }}').

    Returns a list of resolved OS label strings, with duplicates removed.
    """
    if not isinstance(job_config, dict):
        return []

    os_list = []

    runs_on = job_config.get('runs-on')
    strategy = job_config.get('strategy', {})
    matrix = {}
    if isinstance(strategy, dict):
        matrix = strategy.get('matrix', {})
        if not isinstance(matrix, dict):
            matrix = {}

    # --- Attempt to resolve from the matrix first ---
    matrix_var = None
    if isinstance(runs_on, str) and _is_matrix_reference(runs_on):
        matrix_var = _extract_matrix_var_name(runs_on)

    # Case 2: top-level matrix key exists
    if matrix_var and matrix_var in matrix:
        val = matrix[matrix_var]
        if isinstance(val, list):
            os_list.extend([str(v) for v in val])
        elif isinstance(val, str):
            os_list.append(val)

    # Case 4: fallback to 'os' key even without runs-on reference
    if not os_list and 'os' in matrix:
        val = matrix['os']
        if isinstance(val, list):
            os_list.extend([str(v) for v in val])
        elif isinstance(val, str):
            os_list.append(val)

    # Case 3: OS values live only inside 'include' entries
    if not os_list and 'include' in matrix:
        os_list.extend(_extract_os_from_includes(matrix, matrix_var))

    # --- If the matrix did not yield anything, use runs-on directly ---
    if not os_list:
        if isinstance(runs_on, str):
            if not re.search(r'\$\{\{', runs_on):
                # Plain string like 'ubuntu-latest'
                os_list.append(runs_on)
            else:
                # Case 5: expression with fallback, e.g.
                # ${{ vars.ubuntu_small || 'ubuntu-latest' }}
                fallback = _extract_fallback_from_expression(runs_on)
                if fallback:
                    os_list.append(fallback)
                else:
                    # Truly unresolvable; record the expression so it
                    # is visible in the CSV for manual review.
                    os_list.append(runs_on)
        elif isinstance(runs_on, list):
            os_list.extend([str(v) for v in runs_on])

    # Also check for other matrix keys that look OS-related but are
    # named differently (e.g. 'platform', 'operating-system').
    for key in matrix:
        if key in ('os', 'include', 'exclude'):
            continue  # already handled
        if key in ('operating-system', 'platform'):
            val = matrix[key]
            if isinstance(val, list):
                for v in val:
                    sv = str(v).strip().lower()
                    if any(sv.startswith(p) for p in ('ubuntu', 'windows', 'macos', 'self-hosted')):
                        os_list.append(str(v))

    # Deduplicate while preserving order.
    seen = set()
    deduped = []
    for item in os_list:
        normed = _normalize_os(item)
        if normed not in seen:
            seen.add(normed)
            deduped.append(normed)
    return deduped


def has_os_conditionals(job_config):
    """Return True if any step contains runner.os or matrix.os references."""
    steps = job_config.get('steps', [])
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_str = str(step)
        if 'runner.os' in step_str or 'matrix.os' in step_str:
            return True
    return False


def has_include_exclude(job_config):
    """Return True if the strategy.matrix block contains include or
    exclude directives."""
    strategy = job_config.get('strategy', {})
    if not isinstance(strategy, dict):
        return False
    matrix = strategy.get('matrix', {})
    if not isinstance(matrix, dict):
        return False
    return 'include' in matrix or 'exclude' in matrix


def is_self_hosted(os_list):
    """Return True if any label looks like a self-hosted runner."""
    for label in os_list:
        if 'self-hosted' in label.lower():
            return True
    return False


def extract_os_config(workflow_yaml):
    """Parse a workflow YAML and return OS configuration for each Maven job."""
    try:
        workflow = yaml.safe_load(workflow_yaml)
        if not workflow or 'jobs' not in workflow:
            return []

        results = []

        for job_name, job_config in workflow['jobs'].items():
            if not isinstance(job_config, dict):
                continue

            if not is_maven_job(job_config):
                continue

            os_list = resolve_os_list(job_config)
            conditionals = has_os_conditionals(job_config)
            incl_excl = has_include_exclude(job_config)
            self_hosted = is_self_hosted(os_list)

            if os_list:
                results.append({
                    'job_name': job_name,
                    'os_list': os_list,
                    'has_conditionals': conditionals,
                    'has_include_exclude': incl_excl,
                    'is_self_hosted': self_hosted,
                })

        return results

    except Exception as e:
        print(f"  Error parsing YAML: {e}", file=sys.stderr)
        return []


def analyze_repository(slug, token=None):
    """Analyze all workflow files in a single repository."""
    try:
        owner, repo = slug.split('/')
    except ValueError:
        print(f"  Invalid slug format: {slug}", file=sys.stderr)
        return []

    print(f"{slug}...", end=' ', flush=True)

    workflows = fetch_workflow_files(owner, repo, token)
    if not workflows:
        print("No workflows")
        return []

    all_results = []
    for workflow_file in workflows:
        content = download_workflow_content(workflow_file['download_url'])
        if not content:
            continue

        configs = extract_os_config(content)
        for config in configs:
            all_results.append({
                'project_name': slug,
                'workflow_file': workflow_file['name'],
                'job_name': config['job_name'],
                'current_os_config': ','.join(sorted(config['os_list'])),
                'num_os': len(config['os_list']),
                'has_os_conditionals': 'yes' if config['has_conditionals'] else 'no',
                'has_include_exclude': 'yes' if config['has_include_exclude'] else 'no',
                'is_self_hosted': 'yes' if config['is_self_hosted'] else 'no',
            })

    if all_results:
        print(f"{len(all_results)} Maven job(s)")
    else:
        print("No Maven jobs")

    return all_results


def print_statistics(all_results):
    """Print summary statistics to stdout."""
    total = len(all_results)
    if total == 0:
        print("\n  No Maven workflows found across all repositories.")
        return

    os_counts = Counter()
    os_combo_counts = Counter()
    conditional_count = 0
    self_hosted_count = 0

    for result in all_results:
        n = result['num_os']
        os_counts[n] += 1
        os_combo_counts[result['current_os_config']] += 1
        if result['has_os_conditionals'] == 'yes':
            conditional_count += 1
        if result['is_self_hosted'] == 'yes':
            self_hosted_count += 1

    print(f"\n{'=' * 60}")
    print(f" OS COVERAGE STATISTICS  (n = {total} Maven CI jobs)")
    print(f"{'=' * 60}")

    print(f"\n  By number of operating systems:")
    for num_os in sorted(os_counts.keys()):
        count = os_counts[num_os]
        pct = count / total * 100
        bar = '#' * int(pct / 2)
        label = f"{num_os} OS" if num_os == 1 else f"{num_os} OSes"
        print(f"    {label:8s}: {count:4d} ({pct:5.1f}%)  {bar}")

    single = os_counts.get(1, 0)
    multi = total - single
    print(f"\n  Single-OS: {single} / {total} ({100 * single / total:.1f}%)")
    print(f"  Multi-OS:  {multi} / {total} ({100 * multi / total:.1f}%)")

    print(f"\n  Top OS configurations:")
    for combo, count in os_combo_counts.most_common(10):
        pct = count / total * 100
        print(f"    {combo:50s} {count:4d} ({pct:5.1f}%)")

    print(f"\n  Jobs with OS conditionals (runner.os/matrix.os): "
          f"{conditional_count} / {total} ({100 * conditional_count / total:.1f}%)")
    print(f"  Jobs on self-hosted runners: "
          f"{self_hosted_count} / {total} ({100 * self_hosted_count / total:.1f}%)")
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_ci_workflows_v2.py <input_csv> [github_token]")
        print("\nInput CSV should have a 'slug' column with owner/repo values.")
        sys.exit(1)

    input_file = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) > 2 else os.getenv('GITHUB_TOKEN')

    if token:
        print("Using provided GitHub token")
    else:
        print("No GitHub token provided; rate limit is 60 requests/hour.")
        print("  Pass token as 2nd argument or set GITHUB_TOKEN env var.\n")

    # Read input CSV
    projects = []
    try:
        with open(input_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if 'slug' in row and row['slug'].strip():
                    projects.append(row['slug'].strip())
    except Exception as e:
        print(f"Error reading input file: {e}")
        sys.exit(1)

    print(f"Analyzing {len(projects)} repositories...\n")

    # Analyze each repository
    all_results = []
    for i, slug in enumerate(projects, 1):
        print(f"[{i}/{len(projects)}] ", end='')
        results = analyze_repository(slug, token)
        all_results.extend(results)
        time.sleep(0.5)

    # Write output CSV
    output_file = 'workflow_os_analysis.csv'
    fieldnames = [
        'project_name', 'workflow_file', 'job_name',
        'current_os_config', 'num_os',
        'has_os_conditionals', 'has_include_exclude', 'is_self_hosted',
    ]
    written_file = output_file
    try:
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)
    except PermissionError:
        fallback_file = f"workflow_os_analysis_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        print(
            f"\nPermission denied writing to {output_file}. "
            "It may be open in another application."
        )
        print(f"Writing results to {fallback_file} instead.")
        with open(fallback_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)
        written_file = fallback_file

    print(f"\nResults written to {written_file}")

    # Print statistics
    print_statistics(all_results)


if __name__ == '__main__':
    main()