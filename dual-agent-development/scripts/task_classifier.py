from enum import Enum


class Complexity(str, Enum):
    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"
    UNRESOLVED = "UNRESOLVED"


def classify_task(task: str) -> Complexity:
    if not isinstance(task, str) or not task.strip():
        return Complexity.UNRESOLVED
    text = task.lower()
    if any(word in text for word in ("redesign architecture", "cross-module", "across modules", "complex migration", "migrate entire")):
        return Complexity.COMPLEX
    if any(word in text for word in ("two files", "multiple files", "related files", "add tests", "cross-file")):
        return Complexity.MEDIUM
    if any(word in text for word in ("one function", "one config", "one file", "simple bug", "fix one")):
        return Complexity.SIMPLE
    return Complexity.UNRESOLVED
