from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DesignFile(BaseModel):
    path: str = Field(
        ...,
        description="Target file path relative to the project root directory (e.g. 'src/app.py')",
    )
    specification: str = Field(
        ...,
        description="Detailed specifications, functional requirements, and architecture constraints for this specific file.",
    )
    description: Optional[str] = Field(
        default="",
        description="Optional brief description of the file.",
    )
    purpose: Optional[str] = Field(
        default="",
        description="Optional high-level purpose of the file.",
    )


class DesignPlan(BaseModel):
    project_name: Optional[str] = Field(
        default="project",
        description="A unique, URL-safe slug identifying the project.",
    )
    description: Optional[str] = Field(
        default="High-level architectural description.",
        description="High-level architectural description, component interactions, and overall system design.",
    )
    architecture: Optional[str] = Field(
        default="",
        description="Optional detailed architectural overview.",
    )
    files: List[DesignFile] = Field(
        default_factory=list,
        description="A list of source code/configuration files that must be implemented to fulfill the design.",
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="Required external dependencies/packages.",
    )
    devops: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional devops configuration metadata.",
    )


class SecurityFinding(BaseModel):
    severity: str = Field(
        default="medium",
        description="Severity level: 'critical', 'high', 'medium', or 'low'.",
    )
    category: Optional[str] = Field(
        default="General Security",
        description="OWASP/CWE category (e.g. 'Injection', 'Hardcoded Secret', 'Authentication').",
    )
    type: Optional[str] = Field(
        default="Vulnerability",
        description="Classification or type of finding.",
    )
    file: Optional[str] = Field(
        default=None,
        description="Relative file path where the vulnerability was found.",
    )
    line: Optional[int] = Field(
        default=None,
        description="Line number in the source file, if applicable.",
    )
    issue: str = Field(
        default="",
        description="Description of the vulnerability or security flaw.",
    )
    description: Optional[str] = Field(
        default="",
        description="Detailed explanation of the security issue.",
    )
    fix: str = Field(
        default="",
        description="Actionable remediation or code fix recommendation.",
    )
    recommendation: Optional[str] = Field(
        default="",
        description="Guidance on resolving the security finding.",
    )

    @property
    def is_blocking(self) -> bool:
        """True if severity is critical or high."""
        return str(self.severity).lower() in ("critical", "high")


class SecurityReport(BaseModel):
    blocking: bool = Field(
        default=False,
        description="Whether this report contains blocking (critical/high) vulnerabilities.",
    )
    score: Optional[float] = Field(
        default=10.0,
        description="Overall security score (0.0 to 10.0 scale).",
    )
    summary: Optional[str] = Field(
        default="",
        description="Executive summary of the security audit.",
    )
    findings: List[SecurityFinding] = Field(
        default_factory=list,
        description="List of security findings.",
    )

    @property
    def critical_findings(self) -> List[SecurityFinding]:
        return [f for f in self.findings if str(f.severity).lower() == "critical"]

    @property
    def high_findings(self) -> List[SecurityFinding]:
        return [f for f in self.findings if str(f.severity).lower() == "high"]

    @property
    def blocking_findings(self) -> List[SecurityFinding]:
        return [f for f in self.findings if f.is_blocking]


class DevOpsArtifact(BaseModel):
    path: str = Field(
        ...,
        description="Relative target file path for the artifact (e.g. 'Dockerfile', '.github/workflows/ci.yml').",
    )
    content: str = Field(
        ...,
        description="Complete file contents to be materialized.",
    )
    description: Optional[str] = Field(
        default="",
        description="Description or purpose of this DevOps artifact.",
    )


class DevOpsPlan(BaseModel):
    summary: Optional[str] = Field(
        default="",
        description="Executive summary of the deployment and CI/CD architecture.",
    )
    artifacts: List[DevOpsArtifact] = Field(
        default_factory=list,
        description="List of deployment, containerization, and CI/CD artifacts to materialize.",
    )


class DebateVerdict(BaseModel):
    approved: bool = Field(
        default=False,
        description="Whether the architecture plan is approved to proceed.",
    )
    score: float = Field(
        default=3.0,
        description="Overall architectural quality score from 1.0 to 5.0.",
    )
    criteria_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Scores across evaluation dimensions (completeness, modularity, security, feasibility).",
    )
    strengths: List[str] = Field(
        default_factory=list,
        description="Identified architectural strengths.",
    )
    flaws: List[str] = Field(
        default_factory=list,
        description="Identified architectural flaws, gaps, or risks.",
    )
    feedback: Optional[str] = Field(
        default="",
        description="Constructive critique and synthesis notes.",
    )
    concrete_suggestions: List[str] = Field(
        default_factory=list,
        description="Actionable suggestions for refinement.",
    )
    risks: List[str] = Field(
        default_factory=list,
        description="Identified operational or architectural risks.",
    )

    @property
    def is_approved(self) -> bool:
        """True if explicitly approved or score is at least 4.0 with no critical flaws."""
        return self.approved or (self.score >= 4.0 and len(self.flaws) == 0)
