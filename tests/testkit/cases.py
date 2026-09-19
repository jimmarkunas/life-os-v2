from dataclasses import dataclass
from typing import Callable, Iterable

@dataclass(frozen=True)
class Case:
    name: str
    run: Callable[[], None]

def run_cases(cases: Iterable[Case]) -> None:
    for case in cases:
        case.run()
