from __future__ import annotations

import importlib
import sys

import pytest

from scripts import promo_pipeline


class _Module:
    def __init__(self, result):
        self.result, self.seen_argv = result, None

    def main(self):
        self.seen_argv = list(sys.argv)
        return self.result


def test_help_lists_every_command(capsys) -> None:
    assert promo_pipeline.main([]) == 0
    out = capsys.readouterr().out
    for name in promo_pipeline.COMMANDS:
        assert name in out


def test_unknown_command_is_rejected(capsys) -> None:
    assert promo_pipeline.main(["nope"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_dispatch_passes_remaining_args_and_restores_argv() -> None:
    module = _Module(None)
    before = list(sys.argv)
    code = promo_pipeline.main(["extract-pages", "--limit", "5", "--apply", "plan.json"], loader=lambda name: module)
    assert code == 0
    assert module.seen_argv[1:] == ["--limit", "5", "--apply", "plan.json"]
    assert sys.argv == before


def test_exit_code_of_the_underlying_main_is_returned() -> None:
    assert promo_pipeline.main(["discover-pages"], loader=lambda name: _Module(3)) == 3


@pytest.mark.parametrize("command", sorted(promo_pipeline.COMMANDS))
def test_every_command_resolves_to_a_module_with_main(command) -> None:
    module = importlib.import_module(promo_pipeline.COMMANDS[command][0])
    assert callable(getattr(module, "main", None))
