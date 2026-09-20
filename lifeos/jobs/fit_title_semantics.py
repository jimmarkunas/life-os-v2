"""Pure title-semantic classification with caller-injected private patterns."""
from dataclasses import dataclass
import re

CLASSES = ("DIRECT", "ADJACENT", "METHOD_EQUIVALENT", "UNSUPPORTED")

@dataclass(frozen=True)
class TitleSemantics:
    evidence_class: str
    direct_specialization: bool

def _normalize(title: str) -> str:
    title = re.sub("programme", "Program", title, flags=re.IGNORECASE)
    return re.sub(r"[^\w]+", " ", title, flags=re.UNICODE).strip()

def classify_title(title: str, title_patterns: dict[str, tuple[str, ...]],
                   direct_specialization_patterns: tuple[str, ...] = ()) -> TitleSemantics:
    if set(title_patterns) != set(CLASSES): raise ValueError("title patterns must define all classes")
    normalized = _normalize(title)
    matches = lambda pattern: re.search(pattern, normalized, re.IGNORECASE) is not None
    evidence = next((kind for kind in CLASSES if any(matches(p) for p in title_patterns[kind])), "UNSUPPORTED")
    return TitleSemantics(evidence, any(matches(p) for p in direct_specialization_patterns))
