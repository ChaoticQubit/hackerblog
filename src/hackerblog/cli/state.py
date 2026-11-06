"""Global CLI state and the error exit shared by every command module."""

from dataclasses import dataclass
from typing import NoReturn

import typer

from hackerblog.cli.output import emit_error
from hackerblog.core.errors import HackerBlogError
from hackerblog.core.paths import Paths


@dataclass(frozen=True)
class State:
    """Global options shared by every command."""

    paths: Paths
    quiet: bool


def get_state(ctx: typer.Context) -> State:
    """Return the State the root callback stored on the context."""
    state = ctx.find_root().obj
    assert isinstance(state, State)
    return state


def fail(error: HackerBlogError, as_json: bool, extra: dict[str, object] | None = None) -> NoReturn:
    """Report error on the right stream and exit with its code."""
    emit_error(error, as_json, extra)
    raise typer.Exit(int(error.code))
