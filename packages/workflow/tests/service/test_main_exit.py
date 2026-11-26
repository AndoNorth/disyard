"""The service leaves the process itself after a drain, since interpreter finalization can hang."""

import logging
import os

from dsy_workflow.service import __main__ as entry


def test_main_exits_without_finalization(monkeypatch, tmp_path):
    exits = []
    monkeypatch.setattr(entry, "setup", lambda name: None)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls, environ: None))
    monkeypatch.setattr(entry, "real_control", lambda config: ([], None))

    async def drained(*args, **kwargs):
        return None

    monkeypatch.setattr(entry, "serve", drained)
    monkeypatch.setattr(entry.schedule, "default_pass", lambda: None)
    monkeypatch.setattr(logging, "shutdown", lambda: None)
    monkeypatch.setattr(os, "_exit", exits.append)
    entry.main()
    assert exits == [0]
