"""
yaml_os_expander.py

Phase 2, Step 2: Automated OS Matrix Expansion
Implements Algorithm 1 from the paper. Given a GitHub Actions
workflow YAML file, this script:

  1. Parses the YAML and identifies Maven-based jobs.
  2. Determines which target OSes are missing from each job.
  3. Modifies the YAML to add the missing OSes:
     - If no strategy exists: converts single 'runs-on' to a matrix strategy.
     - If strategy exists without os: adds os to the existing matrix.
     - If strategy exists with os: appends missing OSes to existing matrix.os.
  4. Preserves OS-specific conditionals (runner.os, matrix.os).
  5. Skips self-hosted runners and jobs with include/exclude directives.
  6. Validates the modified YAML with yamllint (if available).

Usage:
    # Expand a single file
    python3 yaml_os_expander.py input.yml -o output.yml

    # Expand all YAML files in a directory
    python3 yaml_os_expander.py ./workflows/ -o ./expanded/

    # Batch mode: read a CSV of selected projects and fetch+expand
    python3 yaml_os_expander.py --batch project_candidates.csv [github_token]
"""

import sys
import os
import re
import copy
import csv
import argparse
import requests
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yaml

TARGET_OSES = ['ubuntu-latest', 'windows-latest', 'macos-latest']

# ---------------------------------------------------------------------------
# YAML Parsing Helpers
# ---------------------------------------------------------------------------

def parse_yaml(content):
    """Parse YAML content, returning the dict or None on error."""
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        print(f"  YAML parse error: {e}", file=sys.stderr)
        return None


def is_maven_job(job_config):
    """Return True if any step references Maven."""
    if not isinstance(job_config, dict):
        return False
    steps = job_config.get('steps', [])
    for step in steps:
        if not isinstance(step, dict):
            continue
        run_cmd = str(step.get('run', ''))
        if 'mvn' in run_cmd.lower():
            return True
        uses = str(step.get('uses', ''))
        if 'maven' in uses.lower():
            return True
        name = str(step.get('name', ''))
        if 'maven' in name.lower():
            return True
    return False


def get_current_oses(job_config):
    """Extract the list of OS labels from runs-on and/or matrix.os."""
    oses = []
    runs_on = job_config.get('runs-on')

    strategy = job_config.get('strategy', {})
    matrix = {}
    if isinstance(strategy, dict):
        matrix = strategy.get('matrix', {})
        if not isinstance(matrix, dict):
            matrix = {}

    # Check matrix.os first
    if 'os' in matrix:
        val = matrix['os']
        if isinstance(val, list):
            oses.extend([str(v).strip().lower() for v in val])
        elif isinstance(val, str):
            oses.append(val.strip().lower())

    # If no matrix.os, use runs-on directly (skip if it is a variable ref)
    if not oses and isinstance(runs_on, str):
        if not re.search(r'\$\{\{', runs_on):
            oses.append(runs_on.strip().lower())

    return list(set(oses))


def has_include_exclude(job_config):
    """Check if strategy.matrix has include or exclude directives."""
    strategy = job_config.get('strategy', {})
    if not isinstance(strategy, dict):
        return False
    matrix = strategy.get('matrix', {})
    if not isinstance(matrix, dict):
        return False
    return 'include' in matrix or 'exclude' in matrix


def is_self_hosted(job_config):
    """Check if runs-on references a self-hosted runner."""
    runs_on = job_config.get('runs-on', '')
    return 'self-hosted' in str(runs_on).lower()


def has_matrix_os(job_config):
    """Check if the job already uses a matrix with an 'os' key."""
    strategy = job_config.get('strategy', {})
    if not isinstance(strategy, dict):
        return False
    matrix = strategy.get('matrix', {})
    if not isinstance(matrix, dict):
        return False
    return 'os' in matrix


def has_existing_strategy(job_config):
    """Check if the job already has a strategy block (even without os)."""
    strategy = job_config.get('strategy', {})
    return isinstance(strategy, dict) and len(strategy) > 0


# ---------------------------------------------------------------------------
# YAML Modification (string-level to preserve formatting)
# ---------------------------------------------------------------------------

def _indent_of(line):
    """Return the number of leading spaces in a line."""
    return len(line) - len(line.lstrip())


def expand_single_runs_on_no_strategy(yaml_text, job_name, current_os, missing_oses):
    """Convert a single runs-on value to a matrix strategy.
    
    Use when the job has NO existing strategy block.

    Transforms:
        jobs:
          build:
            runs-on: ubuntu-latest

    Into:
        jobs:
          build:
            runs-on: ${{ matrix.os }}
            strategy:
              fail-fast: false
              matrix:
                os: [ubuntu-latest, windows-latest, macos-latest]
    """
    lines = yaml_text.split('\n')
    new_lines = []
    in_target_job = False
    job_indent = None
    inserted = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Detect job block start
        if stripped.startswith(f'{job_name}:') or stripped == f'{job_name}:':
            in_target_job = True
            job_indent = _indent_of(line)
            new_lines.append(line)
            i += 1
            continue

        # If we are in the target job, look for runs-on
        if in_target_job and not inserted:
            current_indent = _indent_of(line)

            # If we hit a line at the same or lesser indent as the job key,
            # we have left the job block.
            if stripped and current_indent <= job_indent and not stripped.startswith('#'):
                in_target_job = False
                new_lines.append(line)
                i += 1
                continue

            if 'runs-on:' in stripped:
                runs_on_indent = _indent_of(line)
                all_oses = [current_os] + missing_oses
                os_list_str = ', '.join(all_oses)

                # Replace runs-on line
                new_lines.append(f"{' ' * runs_on_indent}runs-on: ${{{{ matrix.os }}}}")
                # Insert new strategy block
                new_lines.append(f"{' ' * runs_on_indent}strategy:")
                new_lines.append(f"{' ' * (runs_on_indent + 2)}fail-fast: false")
                new_lines.append(f"{' ' * (runs_on_indent + 2)}matrix:")
                new_lines.append(f"{' ' * (runs_on_indent + 4)}os: [{os_list_str}]")

                inserted = True
                i += 1
                continue

        new_lines.append(line)
        i += 1

    return '\n'.join(new_lines)


def expand_single_runs_on_with_strategy(yaml_text, job_name, current_os, missing_oses):
    """Add os to an existing strategy/matrix block and update runs-on.
    
    Use when the job HAS an existing strategy block but no os in matrix.

    Transforms:
        jobs:
          build:
            runs-on: ubuntu-latest
            strategy:
              matrix:
                java: ['8', '11', '17']

    Into:
        jobs:
          build:
            runs-on: ${{ matrix.os }}
            strategy:
              fail-fast: false
              matrix:
                java: ['8', '11', '17']
                os: [ubuntu-latest, windows-latest, macos-latest]
    """
    lines = yaml_text.split('\n')
    new_lines = []
    in_target_job = False
    job_indent = None
    replaced_runs_on = False
    inserted_os = False
    added_fail_fast = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Detect job block start
        if stripped.startswith(f'{job_name}:') or stripped == f'{job_name}:':
            in_target_job = True
            job_indent = _indent_of(line)
            new_lines.append(line)
            i += 1
            continue

        if in_target_job:
            current_indent = _indent_of(line)

            # If we hit a line at the same or lesser indent as the job key,
            # we have left the job block.
            if stripped and current_indent <= job_indent and not stripped.startswith('#'):
                in_target_job = False

            # Replace runs-on with matrix reference
            if not replaced_runs_on and 'runs-on:' in stripped:
                runs_on_indent = _indent_of(line)
                new_lines.append(f"{' ' * runs_on_indent}runs-on: ${{{{ matrix.os }}}}")
                replaced_runs_on = True
                i += 1
                continue

            # Add fail-fast: false after strategy: if not already present
            if not added_fail_fast and stripped == 'strategy:':
                strategy_indent = _indent_of(line)
                new_lines.append(line)
                i += 1
                # Check if next line is fail-fast
                if i < len(lines) and 'fail-fast' in lines[i].strip():
                    # Already has fail-fast, keep it
                    added_fail_fast = True
                    new_lines.append(lines[i])
                    i += 1
                else:
                    # Add fail-fast: false
                    new_lines.append(f"{' ' * (strategy_indent + 2)}fail-fast: false")
                    added_fail_fast = True
                continue

            # Find the matrix: block and add os after the last matrix entry
            if not inserted_os and stripped == 'matrix:':
                matrix_indent = _indent_of(line)
                new_lines.append(line)
                i += 1

                # Consume all lines that are part of the matrix block
                # (indent > matrix_indent)
                while i < len(lines):
                    next_line = lines[i]
                    next_stripped = next_line.strip()
                    next_indent = _indent_of(next_line)

                    # Empty lines within the block are kept
                    if next_stripped == '':
                        new_lines.append(next_line)
                        i += 1
                        continue

                    # If indent is greater than matrix:, it is still inside
                    if next_indent > matrix_indent:
                        new_lines.append(next_line)
                        i += 1
                        continue

                    # We have left the matrix block
                    break

                # Insert os line at the same indent as other matrix keys
                all_oses = [current_os] + missing_oses
                os_list_str = ', '.join(all_oses)
                new_lines.append(f"{' ' * (matrix_indent + 2)}os: [{os_list_str}]")
                inserted_os = True
                continue

        new_lines.append(line)
        i += 1

    return '\n'.join(new_lines)


def expand_existing_matrix(yaml_text, job_name, missing_oses):
    """Append missing OSes to an existing matrix.os array.

    Handles both inline arrays:
        os: [ubuntu-latest]
    and block arrays:
        os:
          - ubuntu-latest
    """
    lines = yaml_text.split('\n')
    new_lines = []
    in_target_job = False
    job_indent = None

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Detect job block
        if stripped.startswith(f'{job_name}:') or stripped == f'{job_name}:':
            in_target_job = True
            job_indent = _indent_of(line)

        if in_target_job and stripped and _indent_of(line) <= job_indent and i > 0:
            if not (stripped.startswith(f'{job_name}:') or stripped == f'{job_name}:'):
                in_target_job = False

        if in_target_job:
            # Case 1: inline array   os: [ubuntu-latest, ...]
            match = re.match(r'^(\s*os:\s*)\[(.+)\]\s*$', line)
            if match:
                prefix = match.group(1)
                existing = match.group(2)
                existing_items = [x.strip() for x in existing.split(',')]
                to_add = [o for o in missing_oses if o not in [x.lower() for x in existing_items]]
                if to_add:
                    all_items = existing_items + to_add
                    new_lines.append(f"{prefix}[{', '.join(all_items)}]")
                else:
                    new_lines.append(line)
                i += 1
                continue

            # Case 2: block array start   os:
            #                               - ubuntu-latest
            if stripped == 'os:' or stripped.startswith('os:'):
                os_key_indent = _indent_of(line)
                if stripped == 'os:':
                    new_lines.append(line)
                    i += 1
                    item_indent = None
                    while i < len(lines):
                        next_stripped = lines[i].strip()
                        next_indent = _indent_of(lines[i])
                        if next_stripped.startswith('- ') and (item_indent is None or next_indent == item_indent):
                            item_indent = next_indent
                            new_lines.append(lines[i])
                            i += 1
                        else:
                            break
                    if item_indent is not None:
                        for o in missing_oses:
                            new_lines.append(f"{' ' * item_indent}- {o}")
                    continue
                else:
                    new_lines.append(line)
                    i += 1
                    continue

        new_lines.append(line)
        i += 1

    return '\n'.join(new_lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_yaml_syntax(content):
    """Validate that the content is syntactically correct YAML."""
    try:
        yaml.safe_load(content)
        return True, None
    except yaml.YAMLError as e:
        return False, str(e)


def validate_with_yamllint(filepath):
    """Run yamllint on a file if available. Returns (passed, message)."""
    import subprocess
    try:
        result = subprocess.run(
            ['yamllint', '-d', '{extends: default, rules: {line-length: disable, truthy: disable}}', filepath],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return True, "yamllint passed"
        return False, result.stdout + result.stderr
    except FileNotFoundError:
        return True, "yamllint not installed; skipped"
    except Exception as e:
        return True, f"yamllint error: {e}"


# ---------------------------------------------------------------------------
# Core Expansion Logic
# ---------------------------------------------------------------------------

def expand_workflow(yaml_text):
    """Apply OS expansion to all Maven jobs in a workflow.

    Returns:
        (modified_yaml_text, list_of_changes, list_of_skips)

    Each change is a dict with keys: job_name, action, added_oses.
    Each skip is a dict with keys: job_name, reason.
    """
    parsed = parse_yaml(yaml_text)
    if not parsed or 'jobs' not in parsed:
        return yaml_text, [], [{'job_name': '(all)', 'reason': 'no jobs found or invalid YAML'}]

    changes = []
    skips = []
    modified = yaml_text

    for job_name, job_config in parsed['jobs'].items():
        if not isinstance(job_config, dict):
            continue

        if not is_maven_job(job_config):
            skips.append({'job_name': job_name, 'reason': 'not a Maven job'})
            continue

        if is_self_hosted(job_config):
            skips.append({'job_name': job_name, 'reason': 'self-hosted runner'})
            continue

        if has_include_exclude(job_config):
            skips.append({'job_name': job_name, 'reason': 'matrix has include/exclude directives'})
            continue

        current = get_current_oses(job_config)
        missing = [o for o in TARGET_OSES if o not in current]

        if not missing:
            skips.append({'job_name': job_name, 'reason': 'already covers all target OSes'})
            continue

        if has_matrix_os(job_config):
            # Case 1: matrix already has os -- just append missing ones
            modified = expand_existing_matrix(modified, job_name, missing)
            changes.append({
                'job_name': job_name,
                'action': 'appended to existing matrix.os',
                'current_oses': current,
                'added_oses': missing,
            })
        elif has_existing_strategy(job_config):
            # Case 2: strategy exists (e.g. java matrix) but no os
            # Merge os into the existing matrix
            current_os = current[0] if current else 'ubuntu-latest'
            modified = expand_single_runs_on_with_strategy(modified, job_name, current_os, missing)
            changes.append({
                'job_name': job_name,
                'action': 'added os to existing strategy matrix',
                'current_oses': [current_os],
                'added_oses': missing,
            })
        else:
            # Case 3: no strategy at all -- create new one
            current_os = current[0] if current else 'ubuntu-latest'
            modified = expand_single_runs_on_no_strategy(modified, job_name, current_os, missing)
            changes.append({
                'job_name': job_name,
                'action': 'converted runs-on to matrix',
                'current_oses': [current_os],
                'added_oses': missing,
            })

    return modified, changes, skips


# ---------------------------------------------------------------------------
# File and Batch Processing
# ---------------------------------------------------------------------------

def process_file(input_path, output_path=None):
    """Expand a single YAML file."""
    print(f"\nProcessing: {input_path}")

    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()

    modified, changes, skips = expand_workflow(content)

    if not changes:
        print("  No modifications needed.")
        for s in skips:
            print(f"    Skipped {s['job_name']}: {s['reason']}")
        return False

    # Validate
    valid, err = validate_yaml_syntax(modified)
    if not valid:
        print(f"  ERROR: Modified YAML is invalid: {err}")
        return False

    # Write output
    if output_path is None:
        output_path = input_path
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(modified)

    print(f"  Written to: {output_path}")
    for c in changes:
        print(f"    Job '{c['job_name']}': {c['action']}")
        print(f"      Was: {c['current_oses']}")
        print(f"      Added: {c['added_oses']}")

    # yamllint validation
    lint_ok, lint_msg = validate_with_yamllint(output_path)
    if not lint_ok:
        print(f"  yamllint warnings:\n{lint_msg}")
    else:
        print(f"  {lint_msg}")

    for s in skips:
        if s['reason'] != 'not a Maven job':
            print(f"    Skipped {s['job_name']}: {s['reason']}")

    return True


def process_directory(input_dir, output_dir):
    """Expand all YAML files in a directory."""
    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for fname in sorted(os.listdir(input_dir)):
        if not fname.endswith(('.yml', '.yaml')):
            continue
        inp = os.path.join(input_dir, fname)
        outp = os.path.join(output_dir, fname)
        if process_file(inp, outp):
            count += 1

    print(f"\nExpanded {count} file(s) into {output_dir}")


def fetch_and_expand_from_csv(csv_path, token=None):
    """Batch mode: read candidate projects CSV, fetch their workflow
    files from GitHub, expand them, and save locally."""
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Batch processing {len(rows)} candidate projects...\n")

    base_dir = 'expanded_workflows'
    os.makedirs(base_dir, exist_ok=True)

    summary = []

    for row in rows:
        slug = row['project_name']
        wf_file = row['workflow_file']
        owner, repo = slug.split('/')

        print(f"\n{'=' * 60}")
        print(f"  {slug} / {wf_file}")
        print(f"{'=' * 60}")

        url = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows/{wf_file}"
        headers = {'Accept': 'application/vnd.github.v3.raw'}
        if token:
            headers['Authorization'] = f'token {token}'

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            content = resp.text
        except Exception as e:
            print(f"  Failed to fetch: {e}")
            summary.append({'project': slug, 'status': 'fetch_error', 'details': str(e)})
            time.sleep(0.5)
            continue

        proj_dir = os.path.join(base_dir, slug.replace('/', '__'))
        os.makedirs(proj_dir, exist_ok=True)

        orig_path = os.path.join(proj_dir, f"original_{wf_file}")
        with open(orig_path, 'w', encoding='utf-8') as f:
            f.write(content)

        modified, changes, skips = expand_workflow(content)

        exp_path = os.path.join(proj_dir, f"expanded_{wf_file}")
        with open(exp_path, 'w', encoding='utf-8') as f:
            f.write(modified)

        valid, err = validate_yaml_syntax(modified)

        report_path = os.path.join(proj_dir, 'expansion_report.txt')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"Project: {slug}\n")
            f.write(f"Workflow: {wf_file}\n")
            f.write(f"Current OS: {row.get('current_os_config', 'unknown')}\n")
            f.write(f"YAML valid after expansion: {valid}\n")
            if not valid:
                f.write(f"Validation error: {err}\n")
            f.write(f"\nChanges ({len(changes)}):\n")
            for c in changes:
                f.write(f"  Job '{c['job_name']}': {c['action']}\n")
                f.write(f"    Was: {c['current_oses']}\n")
                f.write(f"    Added: {c['added_oses']}\n")
            f.write(f"\nSkips ({len(skips)}):\n")
            for s in skips:
                f.write(f"  Job '{s['job_name']}': {s['reason']}\n")

        status = 'expanded' if changes else 'no_changes'
        if not valid:
            status = 'invalid_yaml'

        summary.append({
            'project': slug,
            'status': status,
            'changes': len(changes),
            'skips': len(skips),
        })

        print(f"  Status: {status}")
        for c in changes:
            print(f"    + {c['job_name']}: added {c['added_oses']}")

        time.sleep(0.5)

    summary_path = os.path.join(base_dir, 'expansion_summary.csv')
    with open(summary_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['project', 'status', 'changes', 'skips'])
        writer.writeheader()
        for s in summary:
            writer.writerow({k: s.get(k, '') for k in ['project', 'status', 'changes', 'skips']})

    print(f"\n\nBatch summary written to {summary_path}")
    print(f"Expanded files saved under {base_dir}/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Expand OS coverage in GitHub Actions workflow YAML files.'
    )
    parser.add_argument(
        'input',
        help='Path to a YAML file, a directory of YAML files, '
             'or (with --batch) a project_candidates.csv'
    )
    parser.add_argument(
        '-o', '--output',
        help='Output file or directory. Defaults to overwriting input.'
    )
    parser.add_argument(
        '--batch',
        action='store_true',
        help='Batch mode: input is a CSV of candidate projects.'
    )
    parser.add_argument(
        '--token',
        help='GitHub token (or set GITHUB_TOKEN env var).'
    )

    args = parser.parse_args()
    token = args.token or os.getenv('GITHUB_TOKEN')

    if token:
        print("Using GitHub token from environment")
    else:
        print("Warning: No GitHub token found. Rate limit is 60 requests/hour.")

    if args.batch:
        fetch_and_expand_from_csv(args.input, token)
    elif os.path.isdir(args.input):
        output_dir = args.output or os.path.join(args.input, 'expanded')
        process_directory(args.input, output_dir)
    else:
        process_file(args.input, args.output)


if __name__ == '__main__':
    main()