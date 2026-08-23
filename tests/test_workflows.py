"""Guards on the workflow files themselves.

The control loop is well covered by unit tests, but the YAML around it is not
executed by anything until GitHub runs it, and a mistake there is invisible
until someone opens the Actions tab.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = sorted(pathlib.Path(".github/workflows").glob("*.yml"))


@pytest.fixture(params=WORKFLOWS, ids=lambda p: p.name)
def workflow(request):
    return yaml.safe_load(request.param.read_text(encoding="utf-8"))


def test_every_workflow_parses(workflow):
    assert isinstance(workflow, dict)


def test_no_dispatch_option_is_secretly_a_boolean(workflow):
    """YAML reads bare on/off/yes/no as booleans.

    Writing `options: [none, on, off]` produced a dropdown offering "true" and
    "false", and a value the CLI would then reject. Names that cannot be coerced
    are the fix; this is the guard that they stay that way.
    """
    # `on:` is itself parsed as the boolean True, which is why the trigger block
    # is looked up under True rather than under "on".
    triggers = workflow.get(True) or workflow.get("on") or {}
    dispatch = triggers.get("workflow_dispatch") or {}
    for name, spec in (dispatch.get("inputs") or {}).items():
        for option in spec.get("options", []):
            assert isinstance(option, str), (
                f"input {name!r} offers {option!r}, which YAML turned into a "
                f"{type(option).__name__}"
            )
        default = spec.get("default")
        if default is not None:
            assert isinstance(default, str), f"input {name!r} has a non-string default"


def test_dispatch_options_include_their_default(workflow):
    triggers = workflow.get(True) or workflow.get("on") or {}
    dispatch = triggers.get("workflow_dispatch") or {}
    for name, spec in (dispatch.get("inputs") or {}).items():
        options, default = spec.get("options"), spec.get("default")
        if options and default is not None:
            assert default in options, f"input {name!r} defaults to something it does not offer"
