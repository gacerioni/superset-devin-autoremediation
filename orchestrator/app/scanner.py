"""pip-audit scanner: surfaces real CVEs in a checked-out Superset and files GitHub issues for them.

This runs OUTSIDE Devin — CI's job is to find facts; Devin's job is judgment + fix.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from github import Github
from github.GithubException import GithubException

from app.config import settings
from app.logging_config import get_logger
from app.prompts import CVE_ISSUE_BODY_TEMPLATE

log = get_logger(__name__)

WORKSPACE = Path("/workspace/superset")
DEFAULT_REQ = WORKSPACE / "requirements" / "base.txt"


# ---------- types ----------
@dataclass
class CVERecord:
    cve_id: str
    advisory_id: str
    package: str
    current_version: str
    fix_versions: list[str]
    summary: str
    severity: str
    source_file: str

    @property
    def fix_versions_str(self) -> str:
        return ", ".join(self.fix_versions) if self.fix_versions else "(no fix available)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cve_id": self.cve_id,
            "advisory_id": self.advisory_id,
            "package": self.package,
            "current_version": self.current_version,
            "fix_versions": self.fix_versions,
            "fix_versions_str": self.fix_versions_str,
            "summary": self.summary,
            "severity": self.severity,
            "source_file": self.source_file,
        }


@dataclass
class ScanResult:
    total_findings: int
    new_issues: list[dict]
    skipped_duplicates: list[str]
    findings: list[dict]


# ---------- pip-audit ----------
def _clean_requirements(src_path: Path) -> Path:
    """Strip lines pip-audit cannot parse (editable installs, comments, command lines).

    Returns the path to the cleaned file.
    """
    dest = Path("/tmp/clean-requirements.txt")
    with src_path.open() as src, dest.open("w") as dst:
        for raw in src:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith(("#", "-e ", "--", "-r ", "-c ")):
                continue
            # uv-generated files have lines like "    # via flask" — those are comments after a pin
            # and pip-audit handles them. But if a line is *only* indented #, skip.
            if stripped.startswith("# "):
                continue
            dst.write(raw)
    return dest


def _heuristic_severity(summary: str, advisory_id: str) -> str:
    s = (summary or "").lower()
    if "remote code execution" in s or "rce" in s or "arbitrary code" in s:
        return "critical"
    if "denial of service" in s or "dos" in s or "use after free" in s:
        return "high"
    if "cross-origin" in s or "ssrf" in s or "session" in s:
        return "high"
    if "path traversal" in s or "file disclosure" in s:
        return "medium"
    if "sha-1" in s or "weak " in s:
        return "low"
    return "medium"


def run_pip_audit(requirements_path: Path = DEFAULT_REQ) -> list[CVERecord]:
    """Run pip-audit against a requirements file. Returns findings."""
    if not requirements_path.exists():
        raise FileNotFoundError(f"requirements file not found at {requirements_path}")

    clean = _clean_requirements(requirements_path)
    log.info("pip_audit_start", requirements=str(clean), source=str(requirements_path))

    proc = subprocess.run(
        ["pip-audit", "--requirement", str(clean), "--format=json"],
        capture_output=True, text=True, timeout=180,
    )
    # pip-audit returns non-zero when vulns found; that's expected.
    if not proc.stdout:
        raise RuntimeError(
            f"pip-audit produced no stdout. exit={proc.returncode}, stderr={proc.stderr[:400]}"
        )

    data = json.loads(proc.stdout)
    findings: list[CVERecord] = []
    for dep in data.get("dependencies", []):
        for vuln in (dep.get("vulns") or []):
            aliases = vuln.get("aliases") or []
            cve_id = next((a for a in aliases if a.startswith("CVE-")), vuln["id"])
            summary = (vuln.get("description") or "").strip()
            findings.append(CVERecord(
                cve_id=cve_id,
                advisory_id=vuln["id"],
                package=dep["name"],
                current_version=dep["version"],
                fix_versions=vuln.get("fix_versions") or [],
                summary=summary,
                severity=_heuristic_severity(summary, vuln["id"]),
                source_file=requirements_path.name,
            ))
    log.info("pip_audit_done", vuln_count=len(findings))
    return findings


# ---------- GitHub side ----------
def _gh() -> Github:
    return Github(settings.github_token)


def list_existing_remediation_cves(repo_full_name: str) -> set[str]:
    """All CVE IDs already filed under the devin-remediate label (any state)."""
    repo = _gh().get_repo(repo_full_name)
    cves: set[str] = set()
    for state in ("open", "closed"):
        try:
            for issue in repo.get_issues(state=state, labels=[settings.github_remediate_label]):
                # Encoded in title as "Fix CVE-xxxx-yyyy in <pkg>"
                for token in issue.title.replace(":", " ").split():
                    if token.startswith("CVE-"):
                        cves.add(token.strip(",.()[]"))
        except GithubException as e:
            log.warning("gh_list_issues_failed", state=state, status=e.status, msg=str(e))
    return cves


def ensure_label_exists(repo_full_name: str) -> None:
    """Create the devin-remediate label if it doesn't exist."""
    repo = _gh().get_repo(repo_full_name)
    label_name = settings.github_remediate_label
    try:
        repo.get_label(label_name)
    except GithubException:
        try:
            repo.create_label(name=label_name, color="0e8a16",
                              description="Issue queued for autonomous Devin remediation")
            log.info("gh_label_created", label=label_name)
        except GithubException as e:
            log.warning("gh_label_create_failed", status=e.status, msg=str(e))


def ensure_issues_enabled(repo_full_name: str) -> None:
    """Forks have Issues disabled by default. Enable it so the scanner can file CVE issues."""
    repo = _gh().get_repo(repo_full_name)
    if getattr(repo, "has_issues", True):
        return
    try:
        repo.edit(has_issues=True)
        log.info("gh_issues_enabled", repo=repo_full_name)
    except GithubException as e:
        log.warning("gh_issues_enable_failed", status=e.status, msg=str(e))


def create_remediation_issue(repo_full_name: str, finding: CVERecord) -> dict:
    """File a GitHub issue describing the CVE. Returns minimal dict with number/url/title."""
    repo = _gh().get_repo(repo_full_name)
    payload = finding.to_dict()
    body = CVE_ISSUE_BODY_TEMPLATE.format(
        **payload,
        json_payload=json.dumps(payload, indent=2),
    )
    title = f"Fix {finding.cve_id} in {finding.package}"
    issue = repo.create_issue(
        title=title,
        body=body,
        labels=[settings.github_remediate_label],
    )
    log.info("gh_issue_created", number=issue.number, cve=finding.cve_id, package=finding.package)
    return {
        "number": issue.number,
        "url": issue.html_url,
        "title": issue.title,
        "cve_id": finding.cve_id,
        "package": finding.package,
        "severity": finding.severity,
    }


def comment_on_issue(repo_full_name: str, issue_number: int, body: str) -> dict:
    """Append a comment to an existing GitHub issue. Returns minimal dict with id/url."""
    repo = _gh().get_repo(repo_full_name)
    issue = repo.get_issue(number=issue_number)
    comment = issue.create_comment(body)
    log.info("gh_issue_commented", issue=issue_number, comment_id=comment.id)
    return {"id": comment.id, "url": comment.html_url}


def close_issue(repo_full_name: str, issue_number: int, reason: str = "completed") -> None:
    """Close a GitHub issue (used when Devin finishes a remediation)."""
    repo = _gh().get_repo(repo_full_name)
    issue = repo.get_issue(number=issue_number)
    try:
        issue.edit(state="closed", state_reason=reason)
    except TypeError:
        # Older PyGithub versions don't support state_reason
        issue.edit(state="closed")
    log.info("gh_issue_closed", issue=issue_number, reason=reason)


# ---------- orchestration ----------
def run_scan_and_file_issues(
    repo_full_name: str = "",
    requirements_path: Path = DEFAULT_REQ,
    max_issues: int = 10,
) -> ScanResult:
    """Full pipeline: audit -> dedupe -> file issues.

    `max_issues` is a safety cap so a single trigger doesn't open 20 PRs.
    """
    repo = repo_full_name or settings.github_repo
    ensure_issues_enabled(repo)
    ensure_label_exists(repo)

    findings = run_pip_audit(requirements_path)
    existing = list_existing_remediation_cves(repo)
    log.info("scan_dedupe", total_findings=len(findings), existing_cves=len(existing))

    new_issues: list[dict] = []
    skipped: list[str] = []
    for f in findings:
        if f.cve_id in existing:
            skipped.append(f.cve_id)
            continue
        if len(new_issues) >= max_issues:
            log.info("max_issues_reached", cap=max_issues)
            break
        try:
            issue = create_remediation_issue(repo, f)
            new_issues.append(issue)
        except GithubException as e:
            log.error("issue_create_failed", cve=f.cve_id, status=e.status, msg=str(e))

    return ScanResult(
        total_findings=len(findings),
        new_issues=new_issues,
        skipped_duplicates=skipped,
        findings=[f.to_dict() for f in findings],
    )
