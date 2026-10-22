"""Hallucination guard: is every fact in the answer backed by evidence?

The agent must cite sources like [invoice:INV-5102] or [kb:finance_runbook#2].
This check runs on every answer, in both planner modes:
  1. every citation must point to a source actually fetched in this turn;
  2. every concrete fact (document IDs, SAP role names, amounts, dates, durations)
     must appear in the fetched evidence.
It cannot judge paraphrased meaning, only checkable facts. That limitation is
called out in the report; an LLM judge in the evaluation covers some of it.
"""
import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"\[((?:po|invoice|ticket|kb|user|sap_user|approval):[A-Za-z0-9_#\-]+)\]")
FACT_PATTERNS = {
    "id": re.compile(r"\b(?:PO|INV|INC|APR)-\d{4}\b"),
    "role": re.compile(r"\b(?:Z_[A-Z0-9_]+|SAP_ALL|SAP_NEW)\b"),
    "date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "amount": re.compile(r"(?:Rs\.?|₹|INR)\s?(\d(?:[\d,]*\d)?(?:\.\d+)?)"),
    "duration": re.compile(r"\b(\d+)(?:-\d+)?\s+(?:business\s+)?(?:days?|hours?|minutes?)\b"),
}
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class GroundingReport:
    citations: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)
    ungrounded_facts: list[str] = field(default_factory=list)
    has_facts: bool = False

    @property
    def grounded(self) -> bool:
        uncited_facts = self.has_facts and not self.citations
        return not (self.unknown_citations or self.ungrounded_facts or uncited_facts)


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in NUMBER_RE.findall(text)}


ANY_CITATION_RE = re.compile(r"\[([a-z_]+:[A-Za-z0-9_#.\-]+)\]")


def check(answer: str, evidence: dict[str, str], user_text: str = "",
          patterns: dict[str, re.Pattern] | None = None, citation_re: re.Pattern = CITATION_RE) -> GroundingReport:
    """evidence maps source_id -> the text/JSON that source returned.
    Facts the user stated themselves (e.g. the PO number they asked about)
    don't need a citation. Other agents pass their own fact patterns and
    citation format; the defaults are the support desk's."""
    report = GroundingReport()
    report.citations = list(dict.fromkeys(citation_re.findall(answer)))
    report.unknown_citations = [c for c in report.citations if c not in evidence]

    body = citation_re.sub("", answer)
    all_evidence = " ".join(evidence.values())
    evidence_numbers = _numbers(all_evidence)

    for kind, pattern in (patterns or FACT_PATTERNS).items():
        for match in pattern.finditer(body):
            if match.group(0) in user_text:
                continue
            report.has_facts = True
            if kind in ("amount", "duration"):
                value = float(match.group(1).replace(",", ""))
                if value not in evidence_numbers:
                    report.ungrounded_facts.append(match.group(0))
            elif match.group(0) not in all_evidence:
                report.ungrounded_facts.append(match.group(0))
    return report
