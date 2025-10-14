from collections.abc import Callable
from functools import wraps

import typer

from tidal_dl_ng.config import Settings, Tidal
from tidal_dl_ng.constants import CTX_TIDAL


class LoggerWrapped:
    fn_print: Callable = None

    def __init__(self, fn_print: Callable):
        self.fn_print = fn_print

    def debug(self, value):
        self.fn_print(value)

    def warning(self, value):
        self.fn_print(value)

    def info(self, value):
        self.fn_print(value)

    def error(self, value):
        self.fn_print(value)

    def critical(self, value):
        self.fn_print(value)

    def exception(self, value):
        self.fn_print(value)


def skip_login_required(func: Callable):
    """Decorator to ensure a TIDAL context exists while bypassing login."""

    @wraps(func)
    def wrapper(ctx: typer.Context, *args, **kwargs):
        tidal = ctx.obj.get(CTX_TIDAL)

        if tidal is None:
            tidal = Tidal(Settings())
            ctx.obj[CTX_TIDAL] = tidal

        ctx.obj["skip_login"] = True

        return func(ctx, *args, **kwargs)

    return wrapper
