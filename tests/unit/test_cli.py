from __future__ import annotations

import socket

from typer.testing import CliRunner

from sdlc_loop.cli import app, port_in_use


def test_port_in_use_detects_a_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert port_in_use("127.0.0.1", port)
    assert not port_in_use("127.0.0.1", port)  # released


def test_serve_explains_a_busy_port_instead_of_crashing() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = CliRunner().invoke(app, ["serve", "--port", str(port), "--mode", "demo"])
    assert result.exit_code == 1
    assert "already in use" in result.output and "lsof" in result.output
    assert "Traceback" not in result.output
