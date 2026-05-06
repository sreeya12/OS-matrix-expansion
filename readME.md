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

Stages 1, 2, 3a, and 3b are fully automated via Python scripts. The intermediate steps of creating `os-expansion-experiment` branches on forked repositories, triggering workflow runs, and creating fix branches with applied patches are performed manually through the GitHub UI and CLI. These steps involve interacting with GitHub Actions' workflow dispatch API and managing branches across forked repositories, which require manual oversight to handle project-specific variations (e.g., different trigger configurations, credential requirements, branch protection rules).

## Prerequisites

- Python 3.8+
- GitHub personal access token (for API access)
- LLM API key (Groq used for free tier; Anthropic and Gemini also supported with `--provider` flag)

Install dependencies:
```bash
pip install requests pyyaml python-dotenv
```

Create a `.env` file in the project root:
```
GITHUB_TOKEN=ghp_your_github_token_here
GROQ_API_KEY=gsk_your_groq_key_here
```

A `.env` file is already provided in the artifact for simplified access.

## File Descriptions

### Scripts

| File | Description |
|------|-------------|
| `scripts/gen_workflow_csv.py` | Helper script that generates an input CSV from a GitHub repository URL. Takes a repository URL and output directory as arguments, producing a CSV file with the repository slug for use with `ci_workflows.py`. |
| `scripts/ci_workflows.py` | Phase 1 script. Reads a CSV of repository slugs(for multiple open source projects), fetches workflow YAML files via the GitHub API, identifies Maven jobs, and extracts OS configurations (runs-on, matrix.os). Outputs a summary CSV with OS counts and conditional usage for each workflow. |
| `scripts/select_projects.py` | Filters the Phase 1 output to identify candidate projects for OS expansion experiments. Applies selection criteria: single-OS, Maven, recently active, sufficient test count. Outputs a ranked candidate list. |
| `scripts/yaml_os_expander.py` | Phase 2 script. Reads a workflow YAML file and adds missing operating systems (ubuntu-latest, windows-latest, macos-latest) to the build matrix. Handles edge cases: skips self-hosted runners, preserves OS conditionals, skips include/exclude directives. Outputs a modified YAML file and a diff. |
| `scripts/run_workflows.py` | Dispatches GitHub Actions workflow runs via the workflow_dispatch API. Supports both baseline (unmodified) and modified (OS-expanded) execution modes. Records run IDs, statuses, and durations. |
| `scripts/collect_failures.py` | Phase 3a script. Fetches failed workflow runs from GitHub Actions, downloads job logs, and parses them for 16 error patterns (shell errors, Maven failures, Java stack traces, Docker issues, setup-java failures, font errors). Outputs a draft failures CSV with failure type (Q1-Q7), target OS, and error message for each failure. |
| `scripts/llm_os_patcher.py` | Phase 3b script. Reads the failures CSV, fetches the relevant source or YAML file via the GitHub API, builds a structured prompt, and calls an LLM (Groq/Gemini/Anthropic) to generate a git diff. Supports merged patches when multiple OS failures target the same file. Outputs .diff, .prompt, and .response files for each patch attempt. |

### Data

| File | Description |
|------|-------------|
| `data/workflow_os_analysis.csv` | Phase 1 output. OS configuration data for all 737 Maven jobs across 136 projects from the 231-repository dataset. Columns: project_name, workflow_file, job_name, current_os_config, num_os, has_os_conditionals, has_include_exclude, is_self_hosted. |
| `data/project_candidates.csv` | Filtered candidate list from select_projects.py. Contains the 79 projects that meet the selection criteria for OS expansion experiments. |
| `data/selected_projects.csv` | The 9 workflows (across 8 projects) selected for the study. Columns: project_name, workflow_file, job_name, default_branch. |
| `data/outputs/baseline_results.csv` | Baseline execution results. 10 runs per project with unmodified workflows. Columns: project_name, run_id, status, duration, timestamp. |
| `data/outputs/modified_results.csv` | Modified execution results. 10 runs per project with OS-expanded workflows. Columns: project_name, run_id, os, status, duration, timestamp. |
| `data/failures_draft.csv` | Output of collect_failures.py. Contains all categorized failures from modified runs. Columns: project_name, workflow_file, failure_file, error_line, error_message, failure_type, target_os, branch, job_name, run_id, description. |
| `data/patches/patch_results.csv` | Summary of all LLM patch generation attempts. Columns: project_name, failure_type, target_os, file_path, error_message, status, reason, prompt_path, diff_path. |

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
| [PR #1065](https://github.com/mrniko/netty-socketio/pull/1065) | mrniko/netty-socketio | Adds OS matrix + fixes `export` command for Windows compatibility |
| [PR #1547](https://github.com/LibrePDF/OpenPDF/pull/1547) | LibrePDF/OpenPDF | Adds OS matrix + fixes `ls -l` debug step and macOS font test |
| [PR #4510](https://github.com/redis/jedis/pull/4510) | redis/jedis | Adds Windows to OS matrix + fixes Maven `-D` argument quoting |

### PR Descriptions

#### netty-socketio ([PR #1065](https://github.com/mrniko/netty-socketio/pull/1065))

**What is the purpose of this PR:**
Adds Windows and macOS to the CI build matrix. The existing workflow only runs on Ubuntu.

**Expected results:**
Build and tests pass on all three operating systems (Ubuntu, Windows, macOS).

**Actual results:**
The build fails on Windows because the `export` command is a bash built-in not recognized by PowerShell. macOS and Ubuntu pass without issues.

**Description of fix:**
Replaced the inline `export MAVEN_OPTS=...` shell command with a step-level `env:` block, which works across all runner OSes.

---

#### OpenPDF ([PR #1547](https://github.com/LibrePDF/OpenPDF/pull/1547))

**What is the purpose of this PR:**
Adds Windows and macOS to the CI build matrix. The existing workflow only runs on Ubuntu.

**Expected results:**
Build and tests pass on all three operating systems (Ubuntu, Windows, macOS).

**Actual results:**
- **Windows:** The debug step `pwd && ls -l` fails because PowerShell interprets `-l` as the `-LiteralPath` parameter.
- **macOS:** `FontTest.testFontStyleOfStyledFont` fails because macOS's bundled Courier font lacks the OS/2 TrueType table.

**Description of fix:**
1. Added `shell: bash` to the debug step so it runs correctly on Windows.
2. Added `Assumptions.assumeFalse` to skip `testFontStyleOfStyledFont` on macOS where the required font table is unavailable. An alternative approach would be to comment out or remove the `FontFactory.registerDirectories()` call.

---

#### jedis ([PR #4510](https://github.com/redis/jedis/pull/4510))

**What is the purpose of this PR:**
Adds Windows to the CI build matrix. The existing workflow only runs on Ubuntu.

**Expected results:**
- Ubuntu: passes (no change)
- Windows: Maven build and all unit tests pass. The Docker-based Publish Test Results step is skipped via `runner.os == 'Linux'` conditional.

**Actual results:**
- **Maven argument parsing:** PowerShell splits unquoted `-D` arguments at `=`, causing Maven to receive `.dataFile=target/jacoco-ut.exec` as an unknown lifecycle phase.
- **Docker action:** `EnricoMi/publish-unit-test-result-action@v2` only supports Linux runners.

**Note on macOS:** macOS was not included because the workflow uses Java 8 with Temurin, and GitHub's macOS-latest ARM runners do not have Temurin Java 8 builds available.

**Description of fix:**
1. Added `windows-latest` to the OS build matrix.
2. Wrapped Maven `-D` arguments in double quotes so PowerShell treats them as single strings.
3. Added `runner.os == 'Linux'` condition to the Publish Test Results step to skip it on Windows.

## Running the Example

The following example demonstrates the complete automated pipeline on a single project (`iluwatar/java-design-patterns`). The automated steps (Phase 1 analysis, Phase 2 YAML expansion, Phase 3a failure collection, Phase 3b patch generation) take approximately 5 minutes to run. The manual steps (forking, creating branches, triggering GitHub Actions runs) are described but depend on GitHub Actions execution time. 
Create an env file for GITHUB_TOKEN. If preferred any other way, use --token GITHUB_TOKEN at the end of the command.

### Step 1: Generate Input CSV

```bash
python scripts/gen_workflow_csv.py https://github.com/iluwatar/java-design-patterns -o java_design_pattern.csv
```

**Expected output:** A CSV file in the `example_output/` directory containing the repository slug `iluwatar/java-design-patterns`.

### Step 2: Analyze OS Configuration (if analysis required)

```bash
python scripts/ci_workflows.py java-design-pattern.csv
```

**Expected output:**
```
Using provided GitHub token
Analyzing 1 repositories...

[1/1] iluwatar/java-design-patterns... X Maven job(s)

Results written to workflow_os_analysis.csv

Statistics (n=X Maven workflows):
──────────────────────────────────────────────────
  1 OS(es):   X workflows (XX.X%)
  - Single OS:   X / X (XX.X%)
  - Multi-OS:    X / X (XX.X%)
```

**Expected file:** `workflow_os_analysis.csv` containing rows showing the OS configuration for each Maven workflow in the repository.


### Step 3: Expand OS Matrix
The YAML expander runs on the java_design_patterns.csv created from step 1.
```bash
# Run the YAML expander
#for csv input
python scripts/yaml_os_expander.py --batch java_design_pattern.csv 

#for yaml file input
python yaml_os_expander.py input.yml -o output.yml //for yaml file input
```

**Expected output:** A modified YAML file (`maven-ci_modified.yml`) with `os: [ubuntu-latest, windows-latest, macos-latest]` added to the matrix strategy, added to expanded_workflows folder

### Step 4: Manual Steps (Forking, Branch Creation, Workflow Execution)

The following steps are performed manually because they involve interacting with GitHub's repository and Actions infrastructure, which varies per project:

1. **Fork the repository** to your GitHub account.
2. **Add `workflow_dispatch:` trigger** to the workflow file on the fork. This is required for programmatic dispatch and should be removed before submitting any PRs to upstream repositories.
3. **Run baseline:** Trigger the unmodified workflow 10(number can be changed) times using the GitHub Actions UI or the `run_workflows.py` script. Record run IDs and statuses in `baseline_results.csv`.
4. **Create `os-expansion-experiment` branch:** Apply the expanded YAML from Step 3 and push to this branch on the fork.
5. **Run modified workflows:** Trigger the modified workflow 10(number can be changed) times on the `os-expansion-experiment` branch. Record per-OS results in `modified_results.csv`.

These steps produce the baseline and modified results CSV files found in `data/outputs/` for the study's subject projects. The CSV files for baseline and modified runs were created manually for each project because the workflow dispatch process requires project-specific handling (e.g., adding workflow_dispatch triggers, managing fork-specific credential issues, adjusting inter-run timing to avoid GitHub rate limits).
### Step 5: Run baseline & modified workflows
The baseline and modified workflows can be run with the command:
```bash
#for baseline
python scripts/run_workflows.py java_design_patterns.csv --runs 1 --output java_design_pattern_baseline 
#for modified
python scripts/run_workflows.py java_design_patterns_modified.csv --runs 1 --output java_design_pattern_modified 
```
### Step 5: Collect Failures

Once modified workflows have been executed and some runs have failed. Collect the failure logs

```bash
# Collect failures from the os-expansion-experiment branch
python scripts/collect_failures.py java_design_patterns_modified \
  --branch os-expansion-experiment \
  --output java_design_pattern_failures.csv
```

**Expected output:**
```
Collecting failures for 1 project(s)...
Branch: os-expansion-experiment

[1/1] your-username/java-design-patterns (maven-ci.yml)
  Found N failed run(s).
  Processing run ...

Total failures found: N

Breakdown by category:
  Q3: X
  Q4: Y
  Q5: Z
```

**Expected file:** `java_design_pattern_failures.csv` containing one row per distinct failure, with columns for failure type (Q3/Q4/Q5/Q6/Q7), target OS, and error message.

### Step 6: Generate LLM Patch
Groq has been used in this project. Anthropic API can also be used by --api-key flag
The llm_patch_generation is not efficient in the current output due to groq. 
```bash
# Generate patches using Groq (free tier, default provider)
python scripts/llm_os_patcher.py example_failures.csv \
  --output example_patches

#if you have anthropic key
python scripts/llm_os_patcher.py example_failures.csv \
  --output example_patches --api-key ANTHROPIC_API_KEY

# Or use --dry-run to preview prompts without calling the API
python scripts/llm_os_patcher.py example_failures.csv \
  --output example_patches \
  --dry-run
```

**Expected output:**
```
Provider: groq
Model:    llama-3.3-70b-versatile

Input:     N total rows
Patchable: M (Q3: X, Q4: Y)
Skipped:   K

[1/M] your-username/java-design-patterns | Q3 | Windows
  File: .github/workflows/maven-ci.yml
  ...
  Diff saved: example_patches/...diff

Success: M  |  Dry run: 0  |  Errors: 0
```

**Expected files:**
- `example_patches/<project>/*.diff` -- the generated git diff
- `example_patches/<project>/*.prompt` -- the prompt sent to the LLM
- `example_patches/<project>/*.response` -- the raw LLM response
- `example_patches/patch_results.csv` -- summary of all patch attempts

The patcher automatically skips Q5 (missing dependencies), Q6 (generic errors), and Q7 (pre-existing flaky tests) since these are not patchable via code changes.

### Step 7: Apply and Verify (Manual)

After generating patches, the following steps are performed manually:

1. **Create a fix branch** on the forked repository from `os-expansion-experiment`.
2. **Apply the patch:** `git apply patches/<project>/<file>.diff`
3. **Push and trigger** 10 verification runs on the fix branch via the GitHub Actions UI.
4. **Record results:** Verify that the target OS passes and Ubuntu shows no regressions.

For the study's subject projects, verification results are recorded in the patch results table in the paper. The fix branch CSV files were created manually because verification involves inspecting individual workflow run outcomes across multiple OS-job combinations, which requires manual review to distinguish between patched failures, unpatched failures, and infrastructure issues.

### Step 8: Rerun workflows
The csv file is manually created following the initial java_design_pattern csv and replacing the branch.
```bash
python scripts/run_workflows.py java_design_patterns_fixed.csv --runs 1 --output java_design_pattern_fixed //for fixed
```

## Complete Results

The full experimental results for all 9 workflows are available in the `data/` directory. Key findings:

- **RQ1:** 92.1% of Maven CI jobs test on a single OS (ubuntu-latest dominates at 83.7%)
- **RQ2:** Windows fails on every project (0% pass rate); macOS is mixed depending on Java version requirements
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
