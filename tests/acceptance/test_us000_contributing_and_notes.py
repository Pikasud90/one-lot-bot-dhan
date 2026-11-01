"""US-000 AC5 (CONTRIBUTING.md mirrors scripts/gate.sh) and AC6 (timing notes in the story file).

"Exactly the commands in gate.sh" is made mechanical here: every gate step is reduced to a
signature, the text from the tool name onwards (``ruff format --check .``, ``ruff check .``,
``scripts/mypy_ratchet.py``, ``pytest -q`` ...), so the interpreter spelling (``.venv/bin/python
-m`` versus ``"$PY" -m``) and shell quoting do not matter but every flag does.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "gate.sh"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
STORY = ROOT / ".agile" / "stories" / "US-000.md"

FENCE = re.compile(r"^```(?P<lang>[^\n`]*)\n(?P<body>.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
SHELL_TAIL = re.compile(r"\s*(?:\|\||&&|;|\|(?!\|)|2?>|<).*$")


def step_signature(line: str) -> str | None:
    """Reduce one shell line to ``<tool> <args>`` for the four gate tools, else None."""
    text = line.strip()
    if not text or text.startswith("#") or text.split()[0] in {"echo", "printf"}:
        return None
    if match := re.search(r"(?<![\w.-])ruff\s+(?:format|check)\b", text):
        tail = text[match.start() :]
    elif "mypy_ratchet.py" in text:
        start = text.find("scripts/mypy_ratchet.py")
        tail = text[start if start >= 0 else text.find("mypy_ratchet.py") :]
    elif match := re.search(r"(?<![\w.-])pytest(?![\w.-])", text):
        tail = text[match.start() :]
    else:
        return None
    tail = SHELL_TAIL.sub("", tail)
    return " ".join(shlex.split(tail))


def gate_signatures() -> list[str]:
    assert GATE.is_file(), "scripts/gate.sh does not exist yet (US-000 AC4)"
    found = [step_signature(line) for line in GATE.read_text(encoding="utf-8").splitlines()]
    return [sig for sig in found if sig]


def bash_blocks(markdown: str) -> list[str]:
    return [m.group("body") for m in FENCE.finditer(markdown) if m.group("lang").strip() == "bash"]


def block_signatures(body: str) -> list[str]:
    return [sig for sig in (step_signature(line) for line in body.splitlines()) if sig]


def prose_of(markdown: str) -> str:
    return FENCE.sub("", markdown)


# --------------------------------------------------------------------------------------------
# AC5
# --------------------------------------------------------------------------------------------
def test_the_gate_script_yields_the_four_expected_step_signatures():
    # Guards the comparison below against silently comparing nothing with nothing.
    signatures = gate_signatures()
    assert len(signatures) == 4, f"expected four gate steps, parsed {signatures}"
    assert signatures[0] == "ruff format --check ."
    assert signatures[1].startswith("ruff check")
    assert signatures[2].startswith("scripts/mypy_ratchet.py")
    assert signatures[3].startswith("pytest")


def test_contributing_lists_exactly_the_gate_commands_in_a_fenced_bash_block():
    expected = gate_signatures()
    blocks = [
        body
        for body in bash_blocks(CONTRIBUTING.read_text(encoding="utf-8"))
        if block_signatures(body)
    ]
    assert blocks, "CONTRIBUTING.md has no ```bash block with the gate commands"
    listed = [block_signatures(body) for body in blocks]
    assert expected in listed, (
        f"no ```bash block lists exactly the gate.sh commands in order.\n"
        f"gate.sh: {expected}\nCONTRIBUTING.md blocks: {listed}"
    )


def test_contributing_has_no_stale_gate_style_commands_in_any_bash_block():
    allowed = set(gate_signatures())
    stale = [
        sig
        for body in bash_blocks(CONTRIBUTING.read_text(encoding="utf-8"))
        for sig in block_signatures(body)
        if sig not in allowed
    ]
    assert not stale, (
        f"CONTRIBUTING.md shows ruff/pytest/mypy commands that gate.sh does not run: {stale}"
    )


def test_contributing_states_the_ruff_format_policy_in_prose():
    prose = prose_of(CONTRIBUTING.read_text(encoding="utf-8"))
    sentences = re.split(r"(?<=[.!?])\s+|\n\s*\n|\n[-*]\s+", prose)
    policy = [
        s
        for s in sentences
        if re.search(r"ruff\s+format", s, re.IGNORECASE)
        and re.search(r"\b(must|always|never|only|required|policy|before)\b", s, re.IGNORECASE)
    ]
    assert policy, "CONTRIBUTING.md has no sentence stating the `ruff format` policy outside code"


def test_a_doc_test_ties_contributing_to_the_gate_script():
    # AC5 asks for "a doc test": some test outside tests/acceptance must read both files.
    candidates = [
        path
        for path in (ROOT / "tests").rglob("test_*.py")
        if "acceptance" not in path.parts
        and "CONTRIBUTING" in path.read_text(encoding="utf-8")
        and "gate.sh" in path.read_text(encoding="utf-8")
    ]
    assert candidates, "no doc test under tests/ compares CONTRIBUTING.md with scripts/gate.sh"


# --------------------------------------------------------------------------------------------
# AC6
# --------------------------------------------------------------------------------------------
def _recorded_sections() -> str:
    """Story sections where results are recorded; the AC text itself must not count."""
    text = STORY.read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    wanted = ("Notes", "Pairing log", "Review log", "Test log", "Done summary")
    return "\n".join(s for s in sections if s.startswith(wanted))


def test_story_notes_record_wall_seconds_and_the_slowest_test_files():
    recorded = _recorded_sections()
    assert "wall_seconds" in recorded, "no wall_seconds recorded in the story's notes/logs"
    assert "slowest" in recorded, "no 'slowest' test-file table recorded in the story's notes/logs"


def test_story_notes_give_a_number_five_test_files_and_a_proposal():
    recorded = _recorded_sections()
    number = re.search(r"wall_seconds\W{0,8}(\d+(?:\.\d+)?)", recorded)
    assert number and float(number.group(1)) > 0, "wall_seconds has no positive number"
    named = re.findall(r"\btest_[A-Za-z0-9_]+\.py\b", recorded)
    files = {name for name in named if not name.startswith("test_us000_")}
    assert len(files) >= 5, f"expected the five slowest test files by name, found {sorted(files)}"
    assert re.search(r"\bpropos", recorded, re.IGNORECASE), "no proposal to shorten the slow tests"
