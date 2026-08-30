"""End-to-end test for spitch.cmdsock — bring up a real Unix socket
server in a thread, hit it with the client, verify request / response
round-trip + error handling."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from spitch.cmdsock import CmdServer, call, default_socket_path


class _FakeServer:
    """Thin wrapper to stand up a CmdServer on a temp socket and tear
    it down cleanly. Used as a context manager to avoid leaking file
    descriptors when a test fails."""

    def __init__(self, handlers: dict, sock_path: Path):
        self.path = sock_path
        self.server = CmdServer(handlers=handlers, path=sock_path)

    def __enter__(self):
        self.server.start()
        # Tiny wait for the listening thread to be ready.
        deadline = time.time() + 1.0
        while time.time() < deadline and not self.path.exists():
            time.sleep(0.01)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.stop()


class CmdSockTests(unittest.TestCase):
    def test_call_unknown_op_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            with _FakeServer(handlers={}, sock_path=p):
                resp = call("does-not-exist", path=p)
                self.assertFalse(resp["ok"])
                self.assertIn("unknown op", resp["error"])

    def test_call_handler_dispatched(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            with _FakeServer(
                handlers={"echo": lambda req: {"got": req.get("msg", "")}},
                sock_path=p,
            ):
                resp = call("echo", msg="你好", path=p)
                self.assertTrue(resp["ok"])
                self.assertEqual(resp["got"], "你好")

    def test_handler_exception_becomes_error_response(self):
        def boom(req):
            raise ValueError("nope")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            with _FakeServer(handlers={"boom": boom}, sock_path=p):
                resp = call("boom", path=p)
                self.assertFalse(resp["ok"])
                self.assertIn("ValueError", resp["error"])
                self.assertIn("nope", resp["error"])

    def test_call_when_no_socket_raises_connection_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.sock"
            with self.assertRaises(ConnectionError):
                call("ping", path=p)

    def test_socket_is_chmod_600(self):
        import stat as st
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            with _FakeServer(handlers={"ping": lambda r: {}}, sock_path=p):
                # Ensure handshake worked
                resp = call("ping", path=p)
                self.assertTrue(resp["ok"])
                mode = st.S_IMODE(p.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_invalid_json_request(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            with _FakeServer(handlers={"ping": lambda r: {}}, sock_path=p):
                # Bypass call() to send malformed bytes
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(str(p))
                try:
                    s.sendall(b"{not valid\n")
                    line = s.recv(4096).decode("utf-8")
                    self.assertIn("invalid JSON", line)
                finally:
                    s.close()

    def test_stale_socket_replaced_on_start(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"
            # Touch a stale leftover from a "previous daemon crash".
            p.touch()
            with _FakeServer(handlers={"ping": lambda r: {}}, sock_path=p):
                resp = call("ping", path=p)
                self.assertTrue(resp["ok"])

    def test_stop_returns_with_active_subscribe(self):
        """stop() must not hang when a long-lived subscribe is open.

        Regression guard for SIGTERM paths that call CmdServer.stop from
        the daemon main thread — a blocked shutdown() leaves systemd in
        stop-sigterm.
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cmd.sock"

            def _stream(req, send, wait_close):
                wait_close()

            srv = CmdServer(
                handlers={"ping": lambda r: {}},
                stream_handlers={"subscribe": _stream},
                path=p,
            )
            srv.start()
            deadline = time.time() + 1.0
            while time.time() < deadline and not p.exists():
                time.sleep(0.01)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2.0)
            try:
                client.connect(str(p))
                client.sendall(
                    b'{"op":"subscribe","filter":"salmon"}\n'
                )
                ack = client.recv(4096)
                self.assertIn(b"subscribed", ack)
                t0 = time.time()
                srv.stop(timeout=2.0)
                elapsed = time.time() - t0
                self.assertLess(
                    elapsed, 3.0,
                    f"CmdServer.stop hung for {elapsed:.2f}s with open subscribe",
                )
            finally:
                try:
                    client.close()
                except OSError:
                    pass


class DefaultPathTests(unittest.TestCase):
    def test_xdg_runtime_dir_preferred(self):
        prev_runtime = os.environ.get("XDG_RUNTIME_DIR")
        prev_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/9999"
        os.environ.pop("XDG_STATE_HOME", None)
        try:
            self.assertEqual(default_socket_path(), Path("/run/user/9999/spitch.sock"))
        finally:
            if prev_runtime is None:
                del os.environ["XDG_RUNTIME_DIR"]
            else:
                os.environ["XDG_RUNTIME_DIR"] = prev_runtime
            if prev_state is not None:
                os.environ["XDG_STATE_HOME"] = prev_state

    def test_falls_back_to_state_dir_when_runtime_missing(self):
        prev_runtime = os.environ.pop("XDG_RUNTIME_DIR", None)
        prev_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = "/tmp/spitch-test-state"
        try:
            self.assertEqual(
                default_socket_path(),
                Path("/tmp/spitch-test-state/spitch/cmd.sock"),
            )
        finally:
            if prev_runtime is not None:
                os.environ["XDG_RUNTIME_DIR"] = prev_runtime
            if prev_state is None:
                del os.environ["XDG_STATE_HOME"]
            else:
                os.environ["XDG_STATE_HOME"] = prev_state


if __name__ == "__main__":
    unittest.main()
