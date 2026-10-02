import pytest

from custom_components import flipped_energy

__all__ = ["flipped_energy"]


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    reports = terminalreporter.stats.get("passed", [])
    vector_cases = [r for r in reports if "tests/unit/test_vectors.py::" in r.nodeid]
    if vector_cases:
        terminalreporter.write_line(f"{len(vector_cases)} vector cases passed")
