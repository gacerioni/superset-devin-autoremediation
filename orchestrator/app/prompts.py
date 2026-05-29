"""Prompt templates that drive Devin.

Kept terse on purpose — Devin reads structured instructions better than prose.
"""
from __future__ import annotations

CVE_REMEDIATION_PROMPT = """You are remediating a real dependency CVE in the apache/superset codebase.

## CVE details
- **CVE id**: {cve_id}
- **Advisory id**: {advisory_id}
- **Package**: {package}
- **Currently pinned at**: {current_version}
- **Fixed in**: {fix_versions_str}
- **Source file**: requirements/{source_file}
- **Repository**: https://github.com/{repo} (branch: master)

## Summary from the advisory
{summary}

## Your task
1. Clone https://github.com/{repo} (if not already present) and checkout `master`.
2. Locate `{package}` in `requirements/{source_file}` and any other pin files.
3. Decide whether the CVE actually affects this codebase. Look at how `{package}` is used:
   - If the vulnerable codepath is unreachable in Superset's usage → mark `not_applicable` and STOP.
   - Otherwise, proceed with upgrade.
4. Upgrade `{package}` to the minimum non-vulnerable version (prefer the smallest bump from the fix_versions list).
5. Read the upstream CHANGELOG for the new version and identify breaking changes.
6. Search the codebase for call sites of `{package}`. Fix any breaking calls. Update tests if needed.
7. Run the targeted test suite for the modules you touched (NOT the full Superset test suite — pick the targeted modules).
8. Open a PR against `master` of {repo} with title: `fix({package}): bump to <new_version> for {cve_id}`.
9. The PR body MUST include: CVE summary, the breaking changes you encountered (or "none"), list of modified files, the test command you ran with its output.

## Completion criteria (return structured output)
- If you opened a PR → `status: pr_opened`, `pr_url: <url>`, `notes: <summary of what you did>`
- If the CVE doesn't apply → `status: not_applicable`, `pr_url: null`, `notes: <evidence>`
- If you got stuck → `status: failed`, `pr_url: null`, `notes: <what blocked you>`

## Constraints
- Do NOT modify code that is unrelated to this CVE.
- Do NOT bump other dependencies in the same PR.
- Stay under the max_acu_limit. If you're approaching it without a clear path forward, stop and return `failed`.
- Use the GitHub identity provided by the Devin app installation on this repo — do not invent commits as me.
"""


CVE_ISSUE_BODY_TEMPLATE = """## Security advisory: `{cve_id}`

| | |
|--|--|
| **Package** | `{package}` |
| **Current version** | `{current_version}` |
| **Fixed in** | `{fix_versions_str}` |
| **Advisory ID** | `{advisory_id}` |
| **Source** | `requirements/{source_file}` |
| **Severity (heuristic)** | `{severity}` |

### Summary
{summary}

---

This issue was filed automatically by the **Devin auto-remediation orchestrator**.
A Devin session will be dispatched to evaluate this CVE against Superset's actual usage of `{package}`,
and (if applicable) open a PR with the upgrade and any required breaking-change adaptations.

<details>
<summary>Structured payload (used by the orchestrator)</summary>

```json
{json_payload}
```
</details>
"""
