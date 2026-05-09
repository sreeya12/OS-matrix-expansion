import yaml
import requests
import csv
import sys
import time
import os
from collections import Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def fetch_workflow_files(owner, repo, token=None):
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
        print(f" Error fetching workflows for {owner}/{repo}: {e}", file=sys.stderr)
        return []

def download_workflow_content(download_url):
    try:
        response = requests.get(download_url, timeout=10)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f" Error downloading workflow: {e}", file=sys.stderr)
        return None

def is_maven_job(job_config):
    if not isinstance(job_config, dict):
        return False
    
    steps = job_config.get('steps', [])
    for step in steps:
        if not isinstance(step, dict):
            continue
        
        
        run_cmd = step.get('run', '')
        if 'mvn' in str(run_cmd).lower():
            return True
        
        
        uses = step.get('uses', '')
        if 'maven' in str(uses).lower():
            return True
        
        
        name = step.get('name', '')
        if 'maven' in str(name).lower():
            return True
    
    return False

def extract_os_config(workflow_yaml):
    
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
            
            os_list = []
            has_conditionals = False
            
            
            runs_on = job_config.get('runs-on')
            if runs_on:
                if isinstance(runs_on, str):
                    os_list.append(runs_on)
                elif isinstance(runs_on, list):
                    os_list.extend(runs_on)
            
            
            strategy = job_config.get('strategy', {})
            if isinstance(strategy, dict):
                matrix = strategy.get('matrix', {})
                if isinstance(matrix, dict) and 'os' in matrix:
                    matrix_os = matrix['os']
                    if isinstance(matrix_os, list):
                        os_list.extend(matrix_os)
                    elif isinstance(matrix_os, str):
                        os_list.append(matrix_os)
            
            
            steps = job_config.get('steps', [])
            for step in steps:
                if not isinstance(step, dict):
                    continue
                # Only count as OS-conditional if runner.os or matrix.os
                # appears inside an 'if' statement, not in cache keys,
                # artifact names, or other non-conditional contexts.
                step_if = str(step.get('if', ''))
                if 'runner.os' in step_if or 'matrix.os' in step_if:
                    has_conditionals = True
                    break
            
            if os_list:
                results.append({
                    'os_list': list(set(os_list)),  
                    'has_conditionals': has_conditionals
                })
        
        return results
        
    except Exception as e:
        print(f"  Error parsing YAML: {e}", file=sys.stderr)
        return []

def analyze_repository(slug, token=None):
    try:
        owner, repo = slug.split('/')
    except ValueError:
        print(f"  ⚠ Invalid slug format: {slug}", file=sys.stderr)
        return []
    
    print(f"{slug}...", end=' ', flush=True)
    
    workflows = fetch_workflow_files(owner, repo, token)
    if not workflows:
        print(" No workflows")
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
                'current_os_config': ','.join(sorted(config['os_list'])),
                'has_os_conditionals': 'yes' if config['has_conditionals'] else 'no'
            })
    
    if all_results:
        print(f"{len(all_results)} Maven job(s)")
    else:
        print(" No Maven jobs")
    
    return all_results

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_ci_workflows.py <input_csv> [github_token]")
        print("\nInput CSV should have 'slug' column with owner/repo format")
        sys.exit(1)
    
    input_file = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) > 2 else os.getenv('GITHUB_TOKEN')
    
    if token:
        print(" Using provided GitHub token")
    else:
        print(" No GitHub token - rate limit is 60 requests/hour")
        print("   Provide token as 2nd argument for 5000 requests/hour\n")
    
    
    projects = []
    try:
        with open(input_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if 'slug' in row and row['slug']:
                    projects.append(row['slug'])
    except Exception as e:
        print(f" Error reading input file: {e}")
        sys.exit(1)
    
    print(f" Analyzing {len(projects)} repositories...\n")
    
    
    all_results = []
    for i, slug in enumerate(projects, 1):
        print(f"[{i}/{len(projects)}] ", end='')
        results = analyze_repository(slug, token)
        all_results.extend(results)
        time.sleep(0.5)  
    
    
    output_file = 'workflow_os_analysis.csv'
    with open(output_file, 'w', newline='') as f:
        fieldnames = ['project_name', 'workflow_file', 'current_os_config', 'has_os_conditionals']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_results)
    
    print(f"\n Results written to {output_file}")
    
    
    os_counts = Counter()
    for result in all_results:
        num_os = len(result['current_os_config'].split(','))
        os_counts[num_os] += 1
    
    total = len(all_results)
    if total == 0:
        print("\n  No Maven workflows found across all repositories")
        return
    
    print(f"\n Statistics (n={total} Maven workflows):")
    print("─" * 50)
    for num_os in sorted(os_counts.keys()):
        count = os_counts[num_os]
        pct = (count / total * 100) if total > 0 else 0
        print(f"  {num_os} OS(es): {count:3d} workflows ({pct:5.1f}%)")
    
    print(f"  - Single OS:   {os_counts.get(1, 0)} / {total} ({100*os_counts.get(1, 0)/total:.1f}%)")
    print(f"  - Multi-OS:    {total - os_counts.get(1, 0)} / {total} ({100*(total - os_counts.get(1, 0))/total:.1f}%)")

if __name__ == '__main__':
    main()