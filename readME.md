# Automated OS Matrix Expansion in CI Workflows

## Authors
- Batta Sreeya (sbatta2@gmu.edu)
- Pawan Sai Chigurupati (pchigur2@gmu.edu)

George Mason University, CS 691, 2025

## Overview

This artifact accompanies the paper "Automated OS Matrix Expansion in CI Workflows." It contains the complete tooling pipeline for analyzing OS configurations in GitHub Actions workflows, automatically expanding OS coverage, collecting and categorizing failures, and generating LLM-assisted patches.

The pipeline consists of four stages:
1. **Dataset Characterization** (Phase 1): Analyze OS configurations across Java/Maven repositories
2. **YAML OS Expansion** (Phase 2): Automatically modify workflow files to add missing operating systems
3. **Failure Collection** (Phase 3a): Download logs from failed runs and extract error patterns
4. **LLM Patch Generation** (Phase 3b): Generate OS-compatibility patches using LLM-based code generation

## Prerequisites

- Python 3.8+
- GitHub personal access token (for API access)
- LLM API key (Groq used for free tier; Anthropic and Gemini also supported with --LLM flag)

Install dependencies:
```bash
pip install requests pyyaml python-dotenv
```

Create a `.env` file in the project root:
```
GITHUB_TOKEN=ghp_your_github_token_here
GROQ_API_KEY=gsk_your_groq_key_here
```
(env file already provided for simplified project access)
## File Descriptions

### Scripts

| File | Description |
|------|-------------|
| `scripts/ci_workflows.py` | Phase 1 script. Reads a CSV of repository slugs, fetches workflow YAML files via the GitHub API, identifies Maven jobs, and extracts OS configurations (runs-on, matrix.os). Outputs a summary CSV with OS counts and conditional usage for each workflow. |
| `scripts/select_projects.py` | Filters the Phase 1 output to identify candidate projects for OS expansion experiments. Applies selection criteria: single-OS, Maven, recently active, sufficient test count. Outputs a ranked candidate list. |
| `scripts/yaml_OS_expander.py` | Phase 2 script. Reads a workflow YAML file and adds missing operating systems (ubuntu-latest, windows-latest, macos-latest) to the build matrix. Handles edge cases: skips self-hosted runners, preserves OS conditionals, skips include/exclude directives. Outputs a modified YAML file and a diff. |
| `scripts/run_workflows.py` | Dispatches GitHub Actions workflow runs via the workflow_dispatch API. Supports both baseline (unmodified) and modified (OS-expanded) execution modes. Records run IDs, statuses, and durations. |
| `scripts/collect_failures.py` | Phase 3a script. Fetches failed workflow runs from GitHub Actions, downloads job logs, and parses them for 16 error patterns (shell errors, Maven failures, Java stack traces, Docker issues, setup-java failures, font errors). Outputs a draft failures CSV with failure type (Q1-Q7), target OS, and error message for each failure. |
| `scripts/llm_os_patcher.py` | Phase 3b script. Reads the failures CSV, fetches the relevant source or YAML file via the GitHub API, builds a structured prompt, and calls an LLM (Groq/Gemini/Anthropic) to generate a git diff. Supports merged patches when multiple OS failures target the same file. Outputs .diff, .prompt, and .response files for each patch attempt. |

### Data

| File | Description |
|------|-------------|
| `data/workflow_os_analysis.csv` | Phase 1 output. OS configuration data for all 737 Maven jobs across 136 projects from the 231-repository dataset. Columns: project_name, workflow_file, job_name, current_os_config, num_os, has_os_conditionals, has_include_exclude, is_self_hosted. |
| `data/project_candidates.csv` | Filtered candidate list from select_projects.py. Contains the 79 projects that meet the selection criteria for OS expansion experiments. |
| `data/selected_projects.csv` | The 10 workflows (across 9 projects) selected for the study. Columns: project_name, workflow_file, job_name, default_branch. |
| `data/outputs/baseline_results.csv` | Baseline execution results. 10 runs per project with unmodified workflows. Columns: project_name, run_id, status, duration, timestamp. |
| `data/outputs/modified_results.csv` | Modified execution results. 10 runs per project with OS-expanded workflows. Columns: project_name, run_id, os, status, duration, timestamp. |
| `data/failures_draft.csv` | Output of collect_failures.py. Contains all categorized failures from modified runs. Columns: project_name, workflow_file, failure_file, error_line, error_message, failure_type, target_os, branch, job_name, run_id, description. |
| `patches/patch_results.csv` | Summary of all LLM patch generation attempts. Columns: project_name, failure_type, target_os, file_path, error_message, status, reason, prompt_path, diff_path. |

### Patches

| Directory | Description |
|-----------|-------------|
| `data/patches/sreeya12__OpenPDF/` | Generated patches for OpenPDF. Contains .diff (git diff), .prompt (LLM prompt sent), and .response (raw LLM output) files for both the Q3 Windows fix (removing ls -l) and the Q4 macOS fix (font test skip). |
| `data/patches/sreeya12__jedis/` | Generated patch for jedis. Contains files for the Q3 Windows fix (quoting Maven -D arguments). |
| `data/patches/sreeya12__netty-socketio/` | Generated patch for netty-socketio. Contains files for the Q3 Windows fix (export to env: block conversion). |
| `data/patches/sreeya12__graphhopper/` | Generated patch for graphhopper. Contains files for the Q4 Windows fix (MMapDataAccessTest skip on Windows). |
| `data/patches/sreeya12__jjwt/` | Generated patches for jjwt. Contains files for both Q3 Windows and Q3 macOS fixes (OS-conditional package installation), plus a merged patch combining both. |
| `data/patches/sreeya12__curator/` | Generated patch for curator. Contains files for the Q4 Windows fix (TestWatchesBuilder test skip). |

### Draft Pull Requests

| PR | Repository | Description |
|----|-----------|-------------|
| [PR #1](https://github.com/mrniko/netty-socketio/pull/1065) | mrniko/netty-socketio | Adds OS matrix + fixes export command for Windows |
| [PR #2](https://github.com/LibrePDF/OpenPDF/pull/1547) | LibrePDF/OpenPDF | Adds OS matrix + fixes ls command and font test |
| [PR #3](https://github.com/redis/jedis/pull/4510) | redis/jedis | Adds OS matrix + fixes Maven -D argument quoting |

## Running the Example

The following example demonstrates the complete pipeline on a single project (netty-socketio). It takes approximately 5 minutes to run, excluding GitHub Actions execution time.

### Step 1: Analyze OS Configuration

```bash
# Create a single-project input CSV
echo "slug" > example_input.csv
echo "mrniko/netty-socketio" >> example_input.csv
(this can be done by cloning the repository, or can use existing repository from data->outputs->baseline_csv)

# Run the analysis
python scripts/ci_workflows.py example_input.csv
```

**Expected output:**
```
 Using provided GitHub token
 Analyzing 1 repositories...

[1/1] mrniko/netty-socketio... 2 Maven job(s)

 Results written to workflow_os_analysis.csv

 Statistics (n=2 Maven workflows):
──────────────────────────────────────────────────
  1 OS(es):   2 workflows (100.0%)
  - Single OS:   2 / 2 (100.0%)
  - Multi-OS:    0 / 2 (0.0%)
```

**Expected file:** `workflow_os_analysis.csv` containing 2 rows showing both workflows test on ubuntu-latest only.

### Step 2: Expand OS Matrix

```bash
# Download the workflow file
mkdir -p example_workflows
curl -s https://raw.githubusercontent.com/mrniko/netty-socketio/master/.github/workflows/build.yml \
  -o example_workflows/build.yml
(replace username if using forked repo)

# Run the YAML expander
python scripts/yaml_os_expander.py example_workflows/build.yml
```

**Expected output:** A modified YAML file with `os: [ubuntu-latest, windows-latest, macos-latest]` added to the matrix, and a `.diff` file showing the changes.

### Step 3: Collect Failures (after running modified workflows on GitHub)

```bash
# Create the projects CSV
echo "project_name,workflow_file,job_name,default_branch" > example_projects.csv
echo "sreeya12/netty-socketio,build-pr.yml,build,master" >> example_projects.csv

# Collect failures from the os-expansion-experiment branch
python scripts/collect_failures.py example_projects.csv \
  --branch os-expansion-experiment \
  --output example_failures.csv
```

**Expected output:**
```
Collecting failures for 1 project(s)...
Branch: os-expansion-experiment

[1/1] sreeya12/netty-socketio (build-pr.yml)
  Found 1 failed run(s).
  Processing run ...
    Job: build (windows-latest) (OS: Windows)

Total failures found: 1

Breakdown by category:
  Q3: 1

Breakdown by OS:
  Windows: 1
```

**Expected file:** `example_failures.csv` containing 1 row with `failure_type=Q3`, `target_os=Windows`, and error message about `export` not being recognized.

### Step 4: Generate LLM Patch

```bash
# Generate patch using Groq (free tier)
python scripts/llm_os_patcher.py example_failures.csv \
  --output example_patches
```

**Expected output:**
```
Provider: groq
Model:    llama-3.3-70b-versatile

Input:     1 total rows
Patchable: 1 (Q3: 1, Q4: 0)

[1/1] sreeya12/netty-socketio | Q3 | Windows
  File: .github/workflows/build-pr.yml
  ...
  Diff saved: example_patches/sreeya12__netty-socketio/...diff

Success: 1  |  Dry run: 0  |  Errors: 0
```

**Expected files:**
- `example_patches/sreeya12__netty-socketio/*.diff` (the generated git diff)
- `example_patches/sreeya12__netty-socketio/*.prompt` (the prompt sent to the LLM)
- `example_patches/sreeya12__netty-socketio/*.response` (the raw LLM response)
- `example_patches/patch_results.csv` (summary CSV)

### Step 5: Apply and Verify

```bash
# Clone the fork
git clone https://github.com/sreeya12/netty-socketio.git
cd netty-socketio
git checkout os-expansion-experiment

# Apply the patch
git apply ../example_patches/sreeya12__netty-socketio/*.diff
git commit -am "Fix OS compatibility for Windows"
git push origin os-expansion-experiment

# Trigger verification runs via GitHub Actions UI or API
# Expected result: 10/10 pass on all three OSes
```

## Complete Results

The full experimental results for all 10 workflows are available in the `data/` directory. Key findings:

- **RQ1:** 92.1% of Maven CI jobs test on a single OS (ubuntu-latest dominates at 83.7%)
- **RQ2:** Windows fails on every project (0% pass rate); macOS is mixed depending on Java version (8)
- **RQ3:** Shell incompatibility (Q3) is the most common failure at 43.8%, followed by missing dependencies (Q5) at 31.3%
- **RQ4:** LLM-assisted patches resolve 4/7 attempted fixes (57.1%), with Q3 YAML patches at 75% success and Q4 source patches at 33.3%

## Forked Repositories

| Original | Fork | Branch |
|----------|------|--------|
| LibrePDF/OpenPDF | sreeya12/OpenPDF | os-expansion-experiment |
| apache/curator | sreeya12/curator | os-expansion-experiment |
| redis/jedis | sreeya12/jedis | os-expansion-experiment |
| Konloch/bytecode-viewer | sreeya12/bytecode-viewer | os-expansion-experiment |
| mrniko/netty-socketio | sreeya12/netty-socketio | os-expansion-experiment |
| graphhopper/graphhopper | sreeya12/graphhopper | os-expansion-experiment |
| jwtk/jjwt | sreeya12/jjwt | os-expansion-experiment |
| JodaOrg/joda-time | sreeya12/joda-time | os-expansion-experiment |
| apache/fesod | sreeya12/fesod | os-expansion-experiment |

## License

This artifact is provided for academic purposes as part of CS 691 at George Mason University.