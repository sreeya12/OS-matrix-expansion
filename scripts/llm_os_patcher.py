"""
llm_os_patcher.py

Phase 3: Automated Patch Generation (RQ4)
Generates OS-compatibility patches using LLM-based code generation.

Handles two failure categories:
  Q3 - Shell/YAML incompatibility: Fixes workflow YAML files so that
       shell commands, environment variables, and tool invocations
       work across Ubuntu, Windows, and macOS.
  Q4 - Source code OS assumptions: Fixes Java source files that assume
       a specific OS (hardcoded paths, missing fonts, file locking,
       platform-specific system calls).

Rows with failure_type Q5, Q6, Q7, or unknown are automatically skipped.

Pipeline:
  collect_failures.py  -->  failures_draft.csv  -->  llm_os_patcher.py
                                                         |
                                                    patches/*.diff

Input CSV columns (from collect_failures.py output):
  project_name    : e.g. sreeya12/OpenPDF
  workflow_file   : e.g. maven.yml
  failure_file    : path to the file to patch (auto-resolved for Q3)
  error_line      : line number of the error (0 = unknown)
  error_message   : the error text from the log
  failure_type    : Q3 or Q4 (others are skipped)
  target_os       : Windows, macOS, or both
  branch          : branch to fetch file from (default: os-expansion-experiment)

Usage:
    python3 llm_os_patcher.py failures.csv
    python3 llm_os_patcher.py failures.csv --token GITHUB_TOKEN --api-key ANTHROPIC_API_KEY
    python3 llm_os_patcher.py failures.csv --dry-run   # preview prompts without calling API

Output:
    patches/<project>/<safe_name>.diff     -- the generated diff
    patches/<project>/<safe_name>.prompt   -- the prompt sent to the LLM
    patches/<project>/<safe_name>.response -- the raw LLM response
    patches/patch_results.csv             -- summary of all patch attempts
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import base64

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
# Prompt templates
# ---------------------------------------------------------------------------

Q3_YAML_PROMPT = """You are fixing OS compatibility issues in a GitHub Actions workflow YAML file.
The workflow currently runs on Ubuntu but fails on {target_os}.

File: {file_path}
Error on {target_os}: {error_message}

Full workflow file:
{file_content}

IMPORTANT: The error above is only the FIRST failure encountered. There may be OTHER Linux-only commands
in the workflow that would fail on {target_os} after the first error is fixed. Scan the ENTIRE workflow
file and fix ALL platform-specific commands in a single diff, not just the one mentioned in the error.

Look for ALL of the following patterns and fix every occurrence:
1. `export VAR=value` -> replace with step-level or job-level `env:` block
2. `sudo apt-get install ...` or `sudo ...` -> wrap step with `if: runner.os == 'Linux'`
3. `xvfb-run <command>` -> split into two steps: one with `if: runner.os == 'Linux'` that uses xvfb-run,
   and one with `if: runner.os != 'Linux'` that runs the command without xvfb-run
4. `$(pwd)` -> replace with `${{{{ github.workspace }}}}`
5. `ls`, `pwd && ls -l`, `chmod` -> add `shell: bash` to that step, or wrap with OS conditional
6. Maven `-Dproperty=value` without quotes -> wrap in double quotes: `"-Dproperty=value"`
7. `docker` commands -> add `if: runner.os == 'Linux'` to that step
8. `apt-get` without sudo -> wrap step with `if: runner.os == 'Linux'`
9. Shell scripts (`.sh` files) called directly -> add `shell: bash` or wrap with OS conditional

Important constraints:
- Do NOT remove any existing functionality for Ubuntu
- Do NOT change the Maven build commands themselves (only wrap them or add conditionals)
- Preserve all existing matrix variables and their values
- Do NOT modify triggers (on: section) in any way
- Do NOT add `fail-fast: false` or any other strategy options unless directly fixing the error

Output ONLY the git diff. No explanations, no markdown fences."""


Q4_SOURCE_PROMPT = """You are fixing OS compatibility issues in Java source code.
The code currently works on Ubuntu but fails on {target_os}.

File: {file_path}
Error on {target_os}: {error_message}

Source code:
{file_content}

Generate a git diff that fixes this error using cross-platform Java APIs.

Common fixes for Java cross-platform compatibility:
1. Replace hardcoded `/tmp/` paths with `System.getProperty("java.io.tmpdir")`
2. Use `File.separator` or `Paths.get()` instead of hardcoded `/` or `\\`
3. For font-related failures on macOS (e.g., "Table OS/2 does not exist"),
   use bundled test fonts or add OS-conditional test skips:
   `Assumptions.assumeFalse(System.getProperty("os.name").toLowerCase().contains("mac"), "Font not available on macOS")`
4. For file locking issues on Windows (cannot delete folder), ensure streams
   and MappedByteBuffers are closed/unmapped before deletion. Use try-with-resources.
5. For native library failures (UnsatisfiedLinkError), add conditional loading
   or skip tests with `Assumptions.assumeTrue()` based on OS
6. For path separator issues in assertions, normalize paths before comparing
7. For Windows file deletion failures, add retry logic or use `Files.walkFileTree`
   with explicit handle closing

Important constraints:
- Do NOT change test logic or assertions beyond what is needed for OS compatibility
- Prefer `Assumptions.assumeFalse/assumeTrue` over `@Disabled` when the test is valid on some OSes
- Keep the fix minimal and targeted to the specific error
- Import any new classes you reference (e.g., org.junit.jupiter.api.Assumptions)

Output ONLY the git diff. No explanations, no markdown fences."""


Q3_MERGED_PROMPT = """You are fixing OS compatibility issues in a GitHub Actions workflow YAML file.
The workflow currently runs on Ubuntu but fails on BOTH Windows and macOS with different errors.

File: {file_path}

Errors found:
{all_errors}

Full workflow file:
{file_content}

Generate a SINGLE git diff that fixes ALL of the above errors so the workflow runs correctly on Ubuntu, Windows, and macOS simultaneously.

Common fixes for GitHub Actions cross-platform compatibility:
1. Replace `export VAR=value` with an `env:` block on the step or job level
2. Replace `$(pwd)` with `${{{{ github.workspace }}}}`
3. Replace bash-only commands (`ls`, `chmod`, `sudo`, `apt-get`, `wget`) with:
   - Cross-platform alternatives, OR
   - OS-conditional steps using `if: runner.os == 'Linux'` / `if: runner.os != 'Windows'`
4. For Maven `-D` arguments on Windows, wrap them in double quotes:
   `mvn "-Dproperty=value"` instead of `mvn -Dproperty=value`
5. For `docker` commands, add `if: runner.os == 'Linux'` since Docker is not available on all runners
6. For Java setup failures on macOS ARM, change distribution from `adopt` to `temurin` or `zulu`,
   or add `if: matrix.java != 8` conditions for macOS ARM runners
7. For shell script steps (`.sh` files), add `shell: bash` explicitly or wrap in OS conditional
8. Replace hardcoded forward-slash paths with platform-agnostic expressions

Important constraints:
- Do NOT remove any existing functionality for Ubuntu
- Do NOT change the Maven build commands themselves
- Preserve all existing matrix variables and their values
- Address ALL listed errors in a single unified diff
- The diff must be valid and apply cleanly in one pass

Output ONLY the git diff. No explanations, no markdown fences."""


Q4_MERGED_PROMPT = """You are fixing OS compatibility issues in Java source code.
The code currently works on Ubuntu but fails on multiple operating systems with different errors.

File: {file_path}

Errors found:
{all_errors}

Source code:
{file_content}

Generate a SINGLE git diff that fixes ALL of the above errors using cross-platform Java APIs.

Common fixes for Java cross-platform compatibility:
1. Replace hardcoded `/tmp/` paths with `System.getProperty("java.io.tmpdir")`
2. Use `File.separator` or `Paths.get()` instead of hardcoded `/` or `\\`
3. For font-related failures on macOS (e.g., "Table OS/2 does not exist"),
   use bundled test fonts or add OS-conditional test skips
4. For file locking issues on Windows (cannot delete folder), ensure streams
   and MappedByteBuffers are closed/unmapped before deletion
5. For native library failures (UnsatisfiedLinkError), add conditional loading
   or skip tests with `Assumptions.assumeTrue()` based on OS
6. For path separator issues in assertions, normalize paths before comparing
7. For Windows file deletion failures, add retry logic

Important constraints:
- Do NOT change test logic or assertions beyond what is needed for OS compatibility
- Prefer `Assumptions.assumeFalse/assumeTrue` over `@Disabled` when the test is valid on some OSes
- Keep the fix minimal and targeted to the specific errors
- Import any new classes you reference
- Address ALL listed errors in a single unified diff

Output ONLY the git diff. No explanations, no markdown fences."""


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def fetch_file_content(owner, repo, file_path, branch=None, token=None):
    """Fetch a file from GitHub Contents API and return its text content."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    params = {}
    if branch:
        params["ref"] = branch

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 404:
            print(f"    File not found: {owner}/{repo}/{file_path} (branch: {branch})",
                  file=sys.stderr)
            return None
        resp.raise_for_status()
        data = resp.json()

        if data.get("encoding") == "base64":
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        else:
            content = data.get("content", "")

        return content
    except Exception as e:
        print(f"    Error fetching {file_path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------

def format_file_with_line_numbers(content, error_line=0, window=20):
    """Format file content with line numbers.

    For Q3 (YAML), we send the entire file since workflow files are short.
    For Q4 (Java), we extract a window around the error line if the file is large.

    Returns (formatted_content, start_line, end_line).
    """
    lines = content.splitlines()
    total = len(lines)

    if error_line <= 0 or total <= 120:
        # Send the whole file (YAML files, or short Java files)
        numbered = []
        for i, line in enumerate(lines):
            numbered.append(f"{i + 1:4d} | {line}")
        return "\n".join(numbered), 1, total

    # Extract window around error line for large files
    idx = error_line - 1
    start = max(0, idx - window)
    end = min(total, idx + window + 1)

    numbered = []
    for i in range(start, end):
        marker = " >>>" if i == idx else "    "
        numbered.append(f"{i + 1:4d}{marker} | {lines[i]}")

    return "\n".join(numbered), start + 1, end


# ---------------------------------------------------------------------------
# File path resolution
# ---------------------------------------------------------------------------

def resolve_failure_file(row):
    """Determine the correct file path to fetch and patch.

    For Q3 failures, the file to patch is always the workflow YAML.
    For Q4 failures, the file is the Java source file from the error.
    """
    failure_type = row.get("failure_type", "")
    failure_file = row.get("failure_file", "unknown")
    workflow_file = row.get("workflow_file", "")

    if failure_type == "Q3":
        # Q3 = YAML/shell fix. Always patch the workflow file.
        if failure_file.endswith((".yml", ".yaml")):
            return failure_file
        return f".github/workflows/{workflow_file}"

    if failure_type == "Q4":
        # Q4 = source code fix.
        if failure_file and failure_file != "unknown":
            return failure_file
        # Cannot resolve; caller should skip or prompt user
        return None

    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_prompt(failure_type, file_path, error_message, target_os, file_content):
    """Build the LLM prompt from the appropriate template."""
    if failure_type == "Q3":
        template = Q3_YAML_PROMPT
    elif failure_type == "Q4":
        template = Q4_SOURCE_PROMPT
    else:
        return None

    return template.format(
        file_path=file_path,
        error_message=error_message,
        target_os=target_os,
        file_content=file_content,
    )


def build_merged_prompt(failure_type, file_path, errors_list, file_content):
    """Build a merged LLM prompt for multiple errors targeting the same file.

    errors_list: list of (target_os, error_message) tuples
    """
    all_errors = "\n".join(
        f"  - {os_name}: {msg}" for os_name, msg in errors_list
    )

    if failure_type == "Q3":
        template = Q3_MERGED_PROMPT
    elif failure_type == "Q4":
        template = Q4_MERGED_PROMPT
    else:
        return None

    return template.format(
        file_path=file_path,
        all_errors=all_errors,
        file_content=file_content,
    )


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

def call_anthropic_api(prompt, api_key, model="claude-sonnet-4-20250514"):
    """Call the Anthropic Messages API and return the text response."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
        return "\n".join(text_parts)
    except requests.exceptions.HTTPError as e:
        print(f"    Anthropic API HTTP error: {e}", file=sys.stderr)
        if e.response is not None:
            print(f"    Response body: {e.response.text[:500]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    Anthropic API error: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Google Gemini API
# ---------------------------------------------------------------------------

def call_gemini_api(prompt, api_key, model="gemini-2.0-flash"):
    """Call the Google Gemini API and return the text response."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
    }
    params = {
        "key": api_key,
    }
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.2,
        },
    }

    try:
        resp = requests.post(url, headers=headers, params=params, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        # Extract text from Gemini response
        candidates = data.get("candidates", [])
        if not candidates:
            print(f"    Gemini returned no candidates.", file=sys.stderr)
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = []
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])

        if not text_parts:
            print(f"    Gemini returned no text content.", file=sys.stderr)
            return None

        return "\n".join(text_parts)
    except requests.exceptions.HTTPError as e:
        print(f"    Gemini API HTTP error: {e}", file=sys.stderr)
        if e.response is not None:
            print(f"    Response body: {e.response.text[:500]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    Gemini API error: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Groq API (Llama models, free tier)
# ---------------------------------------------------------------------------

def call_groq_api(prompt, api_key, model="llama-3.3-70b-versatile", max_retries=3):
    """Call the Groq API with automatic retry on rate limits.
    
    Groq uses the OpenAI-compatible API format.
    Free tier: 12,000 TPM for llama-3.3-70b-versatile.
    On 429, parses the retry delay from the error and waits.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8192,
        "temperature": 0.2,
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=180)

            if resp.status_code == 429:
                # Parse wait time from error message
                wait_time = 60  # default fallback
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message", "")
                    # Extract "Please try again in 27.79s"
                    wait_match = re.search(r"try again in ([\d.]+)s", err_msg)
                    if wait_match:
                        wait_time = float(wait_match.group(1)) + 2  # add buffer
                except Exception:
                    pass

                if attempt < max_retries:
                    print(f"    Rate limited. Waiting {wait_time:.0f}s "
                          f"(attempt {attempt + 1}/{max_retries})...",
                          file=sys.stderr)
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"    Rate limited after {max_retries} retries. Giving up.",
                          file=sys.stderr)
                    return None

            resp.raise_for_status()
            data = resp.json()

            choices = data.get("choices", [])
            if not choices:
                print(f"    Groq returned no choices.", file=sys.stderr)
                return None

            message = choices[0].get("message", {})
            content = message.get("content", "")

            if not content:
                print(f"    Groq returned empty content.", file=sys.stderr)
                return None

            return content
        except requests.exceptions.HTTPError as e:
            print(f"    Groq API HTTP error: {e}", file=sys.stderr)
            if e.response is not None:
                print(f"    Response body: {e.response.text[:500]}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"    Groq API error: {e}", file=sys.stderr)
            return None

    return None


# ---------------------------------------------------------------------------
# Unified LLM caller
# ---------------------------------------------------------------------------

def call_llm(prompt, provider, api_key, model=None):
    """Call the appropriate LLM API based on provider."""
    if provider == "anthropic":
        m = model or "claude-sonnet-4-20250514"
        return call_anthropic_api(prompt, api_key, model=m)
    elif provider == "gemini":
        m = model or "gemini-2.0-flash"
        return call_gemini_api(prompt, api_key, model=m)
    elif provider == "groq":
        m = model or "llama-3.3-70b-versatile"
        return call_groq_api(prompt, api_key, model=m)
    else:
        print(f"    Unknown provider: {provider}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Diff extraction
# ---------------------------------------------------------------------------

def extract_diff_from_response(response_text):
    """Extract the git diff from the LLM response.

    Handles: fenced code blocks, raw diff output, or mixed text+diff.
    """
    if not response_text:
        return None

    # Try fenced code block (```diff ... ``` or ``` ... ```)
    fence_pattern = r"```(?:diff)?\s*\n(.*?)```"
    match = re.search(fence_pattern, response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If the response starts with diff/--- lines, take it as-is
    lines = response_text.strip().splitlines()
    if lines and (lines[0].startswith("diff ") or lines[0].startswith("---")):
        return response_text.strip()

    # Try to find diff lines anywhere in the response
    diff_lines = []
    in_diff = False
    for line in lines:
        if line.startswith("diff ") or line.startswith("---"):
            in_diff = True
        if in_diff:
            diff_lines.append(line)

    if diff_lines:
        return "\n".join(diff_lines)

    # Last resort: return the whole thing for manual inspection
    return response_text.strip()


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_failure(row, github_token, api_key, output_dir, provider="gemini",
                    model=None, dry_run=False):
    """Process a single Q3 or Q4 failure entry and generate a patch."""
    project = row["project_name"]
    error_message = row["error_message"]
    failure_type = row.get("failure_type", "")
    target_os = row.get("target_os", "Windows and macOS")
    branch = row.get("branch", "os-expansion-experiment")

    # Parse owner/repo
    parts = project.split("/")
    if len(parts) != 2:
        print(f"  Invalid project_name: {project}", file=sys.stderr)
        return {"status": "error", "reason": "invalid project_name"}

    owner, repo = parts

    # Resolve file path
    file_path = resolve_failure_file(row)
    if not file_path:
        print(f"  Cannot resolve file path for this failure.", file=sys.stderr)
        print(f"  Set 'failure_file' in the CSV to the correct path.", file=sys.stderr)
        return {"status": "error", "reason": "unresolved_file_path"}

    print(f"  File: {file_path}")
    print(f"  Error: {error_message[:80]}...")

    # Fetch file content
    print(f"  Fetching from {project} (branch: {branch})...")
    content = fetch_file_content(owner, repo, file_path, branch=branch, token=github_token)

    # If not found on experiment branch, try default branch
    if content is None:
        default_branch = row.get("default_branch", "main")
        print(f"  Retrying on branch: {default_branch}...")
        content = fetch_file_content(owner, repo, file_path, branch=default_branch,
                                     token=github_token)

    if content is None:
        return {"status": "error", "reason": "file_not_found"}

    # Extract context
    error_line = 0
    try:
        error_line = int(row.get("error_line", "0") or "0")
    except ValueError:
        pass

    formatted_content, ctx_start, ctx_end = format_file_with_line_numbers(
        content, error_line
    )
    print(f"  Context: lines {ctx_start}-{ctx_end} ({ctx_end - ctx_start + 1} lines)")

    # Build prompt
    prompt = build_prompt(failure_type, file_path, error_message, target_os,
                          formatted_content)
    if prompt is None:
        return {"status": "error", "reason": "unsupported_failure_type"}

    # Save prompt
    project_dir = os.path.join(output_dir, project.replace("/", "__"))
    os.makedirs(project_dir, exist_ok=True)

    safe_name = re.sub(r'[^\w\-.]', '_', file_path) + f"__{target_os}"
    prompt_path = os.path.join(project_dir, f"{safe_name}.prompt")
    with open(prompt_path, "w") as f:
        f.write(prompt)
    print(f"  Prompt saved: {prompt_path}")

    if dry_run:
        print(f"  [DRY RUN] Skipping API call.")
        return {
            "status": "dry_run",
            "prompt_path": prompt_path,
            "file_path": file_path,
        }

    # Call LLM
    print(f"  Calling {provider} API...")
    response = call_llm(prompt, provider, api_key, model=model)
    if response is None:
        return {"status": "error", "reason": "api_call_failed"}

    # Save raw response
    raw_path = os.path.join(project_dir, f"{safe_name}.response")
    with open(raw_path, "w") as f:
        f.write(response)

    # Extract diff
    diff = extract_diff_from_response(response)
    if diff is None:
        return {"status": "error", "reason": "no_diff_in_response"}

    diff_path = os.path.join(project_dir, f"{safe_name}.diff")
    with open(diff_path, "w") as f:
        f.write(diff)
    print(f"  Diff saved: {diff_path}")

    return {
        "status": "success",
        "prompt_path": prompt_path,
        "diff_path": diff_path,
        "raw_response_path": raw_path,
        "context_lines": f"{ctx_start}-{ctx_end}",
        "file_path": file_path,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate OS-compatibility patches for Q3 (YAML) and Q4 (source code) failures."
    )
    parser.add_argument(
        "failures_csv",
        help="CSV file with failure entries (from collect_failures.py).",
    )
    parser.add_argument(
        "--token",
        help="GitHub personal access token (or set GITHUB_TOKEN env var).",
    )
    parser.add_argument(
        "--api-key",
        help="LLM API key (or set GEMINI_API_KEY / ANTHROPIC_API_KEY env var).",
    )
    parser.add_argument(
        "-o", "--output",
        default="patches",
        help="Output directory for diffs and prompts (default: patches/).",
    )
    parser.add_argument(
        "--provider",
        default="groq",
        choices=["groq", "gemini", "anthropic"],
        help="LLM provider to use (default: groq).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override (default: gemini-2.0-flash or claude-sonnet-4-20250514).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build prompts and save them without calling the API.",
    )

    args = parser.parse_args()
    github_token = args.token or os.getenv("GITHUB_TOKEN")
    provider = args.provider

    # Resolve API key based on provider
    if args.api_key:
        api_key = args.api_key
    elif provider == "groq":
        api_key = os.getenv("GROQ_API_KEY")
    elif provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
    else:
        api_key = os.getenv("ANTHROPIC_API_KEY")

    if not github_token:
        print("Error: GitHub token required. Use --token or set GITHUB_TOKEN.",
              file=sys.stderr)
        sys.exit(1)
    if not api_key and not args.dry_run:
        env_vars = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        env_var = env_vars.get(provider, "API_KEY")
        print(f"Error: API key required. Use --api-key or set {env_var}.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Provider: {provider}")
    model_name = args.model or {"groq": "llama-3.3-70b-versatile", "gemini": "gemini-2.0-flash", "anthropic": "claude-sonnet-4-20250514"}.get(provider, "unknown")
    print(f"Model:    {model_name}")
    print()

    # Read failures CSV
    all_rows = []
    try:
        with open(args.failures_csv, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                all_rows.append(row)
    except Exception as e:
        print(f"Error reading {args.failures_csv}: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter to Q3 and Q4 only
    patchable = []
    skipped = []
    for row in all_rows:
        ft = row.get("failure_type", "")
        if ft in ("Q3", "Q4"):
            # Skip rows with unknown file path for Q4
            if ft == "Q4" and row.get("failure_file", "unknown") == "unknown":
                skipped.append((row, "Q4 with unknown file path"))
            else:
                patchable.append(row)
        else:
            skipped.append((row, f"failure_type={ft}, not patchable"))

    print(f"Input:     {len(all_rows)} total rows")
    print(f"Patchable: {len(patchable)} "
          f"(Q3: {sum(1 for r in patchable if r['failure_type']=='Q3')}, "
          f"Q4: {sum(1 for r in patchable if r['failure_type']=='Q4')})")
    print(f"Skipped:   {len(skipped)}")
    for row, reason in skipped:
        proj = row.get("project_name", "?")
        msg = row.get("error_message", "?")[:50]
        print(f"  - {proj}: {reason} ({msg}...)")
    print()

    if not patchable:
        print("No patchable failures found.")
        sys.exit(0)

    os.makedirs(args.output, exist_ok=True)

    # Process each patchable failure
    results = []
    for i, row in enumerate(patchable, 1):
        project = row.get("project_name", "unknown")
        failure_type = row.get("failure_type", "?")
        target_os = row.get("target_os", "?")
        print(f"[{i}/{len(patchable)}] {project} | {failure_type} | {target_os}")

        result = process_failure(row, github_token, api_key, args.output,
                                 provider=provider, model=args.model,
                                 dry_run=args.dry_run)
        result["project_name"] = project
        result["failure_type"] = failure_type
        result["target_os"] = target_os
        result["error_message"] = row.get("error_message", "")[:100]
        results.append(result)

        # Delay between API calls (Groq free tier needs longer gaps)
        if i < len(patchable) and not args.dry_run:
            if provider == "groq":
                delay = 45  # Groq free tier: 12k TPM, need ~45s between large prompts
                print(f"  Waiting {delay}s for rate limit...")
                time.sleep(delay)
            else:
                time.sleep(2)

    # -----------------------------------------------------------------------
    # Generate merged patches for files with multiple OS-specific failures
    # -----------------------------------------------------------------------
    merged_results = []

    # Group successful patches by (project, resolved_file_path)
    file_groups = {}
    for r in results:
        if r["status"] != "success":
            continue
        key = (r["project_name"], r.get("file_path", ""))
        if key not in file_groups:
            file_groups[key] = []
        file_groups[key].append(r)

    # For groups with 2+ patches on the same file, generate a merged patch
    for (project, file_path), group in file_groups.items():
        if len(group) < 2:
            continue

        print(f"\n--- Generating merged patch for {project} / {file_path} ---")
        print(f"  Combining {len(group)} individual patches:")
        for g in group:
            print(f"    - {g['target_os']}: {g.get('error_message', '')[:60]}...")

        # Determine failure type (should be the same for all in group)
        failure_type = group[0]["failure_type"]

        # Collect all errors
        errors_list = []
        for g in group:
            errors_list.append((g["target_os"], g.get("error_message", "")))

        # Parse owner/repo
        parts = project.split("/")
        if len(parts) != 2:
            continue
        owner, repo = parts

        # Fetch file content (use branch from first group entry)
        branch = "os-expansion-experiment"
        content = fetch_file_content(owner, repo, file_path, branch=branch,
                                     token=github_token)
        if content is None:
            content = fetch_file_content(owner, repo, file_path, branch="main",
                                         token=github_token)
        if content is None:
            print(f"  Could not fetch {file_path} for merged patch.")
            continue

        # Format content
        formatted_content, ctx_start, ctx_end = format_file_with_line_numbers(content)

        # Build merged prompt
        merged_prompt = build_merged_prompt(failure_type, file_path, errors_list,
                                            formatted_content)
        if merged_prompt is None:
            continue

        # Save merged prompt
        project_dir = os.path.join(args.output, project.replace("/", "__"))
        os.makedirs(project_dir, exist_ok=True)
        safe_name = re.sub(r'[^\w\-.]', '_', file_path) + "__MERGED"
        merged_prompt_path = os.path.join(project_dir, f"{safe_name}.prompt")
        with open(merged_prompt_path, "w") as f:
            f.write(merged_prompt)
        print(f"  Merged prompt saved: {merged_prompt_path}")

        if args.dry_run:
            print(f"  [DRY RUN] Skipping API call for merged patch.")
            merged_results.append({
                "project_name": project,
                "failure_type": failure_type,
                "target_os": "Windows+macOS",
                "file_path": file_path,
                "status": "dry_run",
                "prompt_path": merged_prompt_path,
            })
            continue

        # Call LLM
        print(f"  Calling {provider} API for merged patch...")
        if provider == "groq":
            print(f"  Waiting 45s for rate limit...")
            time.sleep(45)

        response = call_llm(merged_prompt, provider, api_key, model=args.model)
        if response is None:
            print(f"  Merged patch API call failed.")
            merged_results.append({
                "project_name": project,
                "failure_type": failure_type,
                "target_os": "Windows+macOS",
                "file_path": file_path,
                "status": "error",
                "reason": "api_call_failed",
            })
            continue

        # Save response
        merged_raw_path = os.path.join(project_dir, f"{safe_name}.response")
        with open(merged_raw_path, "w") as f:
            f.write(response)

        # Extract diff
        diff = extract_diff_from_response(response)
        if diff is None:
            merged_results.append({
                "project_name": project,
                "failure_type": failure_type,
                "target_os": "Windows+macOS",
                "file_path": file_path,
                "status": "error",
                "reason": "no_diff_in_response",
            })
            continue

        merged_diff_path = os.path.join(project_dir, f"{safe_name}.diff")
        with open(merged_diff_path, "w") as f:
            f.write(diff)
        print(f"  Merged diff saved: {merged_diff_path}")

        merged_results.append({
            "project_name": project,
            "failure_type": failure_type,
            "target_os": "Windows+macOS",
            "file_path": file_path,
            "status": "success",
            "prompt_path": merged_prompt_path,
            "diff_path": merged_diff_path,
            "raw_response_path": merged_raw_path,
        })

    # Combine all results
    all_results = results + merged_results

    # Write summary CSV
    summary_path = os.path.join(args.output, "patch_results.csv")
    fieldnames = [
        "project_name", "failure_type", "target_os", "file_path",
        "error_message", "status", "reason",
        "prompt_path", "diff_path", "raw_response_path", "context_lines",
    ]
    with open(summary_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_results)

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Results written to {summary_path}")
    success = sum(1 for r in all_results if r["status"] == "success")
    dry = sum(1 for r in all_results if r["status"] == "dry_run")
    errors = sum(1 for r in all_results if r["status"] == "error")
    merged_count = sum(1 for r in merged_results if r["status"] == "success")
    print(f"Success: {success} (incl. {merged_count} merged)  |  Dry run: {dry}  |  Errors: {errors}")

    if success > 0:
        print(f"\nGenerated diffs:")
        for r in all_results:
            if r["status"] == "success":
                label = " [MERGED]" if "MERGED" in r.get("diff_path", "") else ""
                print(f"  {r.get('diff_path', '?')}{label}")

    if errors > 0:
        print(f"\nFailed entries:")
        for r in all_results:
            if r["status"] == "error":
                print(f"  {r['project_name']} ({r['failure_type']}, {r['target_os']}): "
                      f"{r.get('reason', '?')}")



if __name__ == "__main__":
    main()