"""
GitHub Actions annotations: the one place a finding that needs a human is
surfaced.

An annotation printed with `::error::` or `::warning::` is pinned to the run's
summary page, and GitHub's own "Run failed" email links straight to it — so a
failure that turns the run red already reaches the owner's inbox without this
project sending any mail of its own. Outside Actions the same line is simply
printed, which keeps local runs readable.

Standard library only: agent/packet.py's pre-flight imports this before
daily.yml has installed anything (tests/test_workflow_preflight.py).
"""


def _escape_data(text: str) -> str:
    # GitHub's workflow-command encoding. A raw newline would end the
    # annotation at the first line and print the rest as plain log output.
    return (str(text).replace("%", "%25")
            .replace("\r", "%0D").replace("\n", "%0A"))


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _emit(level: str, title: str, message: str) -> str:
    line = f"::{level} title={_escape_property(title)}::{_escape_data(message)}"
    print(line, flush=True)
    return line


def error(title: str, message: str) -> str:
    """Something a human has to act on. Returns the printed line (for tests)."""
    return _emit("error", title, message)


def warning(title: str, message: str) -> str:
    """Worth seeing on the run page, but not by itself a reason to act."""
    return _emit("warning", title, message)
