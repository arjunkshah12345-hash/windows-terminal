import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from windows_terminal.cli import parser
from windows_terminal.control import Computer, text_chords
from windows_terminal.mcp import TOOLS, handle, serve
from windows_terminal.qmp import QMPClient, QMPError
from windows_terminal.render import detect_protocol, image_geometry, render
from windows_terminal.viewer import InputDecoder


class QMPTests(unittest.TestCase):
    def server(self, handler):
        temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "qmp.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(2)
        errors = []

        def work():
            try:
                connection, _ = listener.accept()
                with connection, connection.makefile("rwb") as stream:
                    handler(stream)
            except Exception as error:
                errors.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def check():
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(check)
        return path

    @staticmethod
    def handshake(stream):
        stream.write(b'{"QMP": {}}\n')
        stream.flush()
        request = json.loads(stream.readline())
        stream.write(json.dumps({"id": request["id"], "return": {}}).encode() + b"\n")
        stream.flush()

    def test_capabilities_events_and_ids(self):
        def respond(stream):
            self.handshake(stream)
            request = json.loads(stream.readline())
            self.assertEqual(request["execute"], "query-status")
            stream.write(b'{"event": "RESUME"}\n')
            stream.write(json.dumps({"return": {"status": "running"}, "id": request["id"]}).encode() + b"\n")
            stream.flush()

        with QMPClient(self.server(respond)) as client:
            self.assertEqual(client.execute("query-status"), {"status": "running"})

    def test_command_error(self):
        def respond(stream):
            self.handshake(stream)
            request = json.loads(stream.readline())
            stream.write(json.dumps({"error": {"desc": "bad command"}, "id": request["id"]}).encode() + b"\n")
            stream.flush()

        with QMPClient(self.server(respond)) as client:
            with self.assertRaisesRegex(QMPError, "bad command"):
                client.execute("bad")

    def test_non_object_response(self):
        def respond(stream):
            stream.write(b"[]\n")
            stream.flush()

        with self.assertRaisesRegex(QMPError, "must be an object"):
            with QMPClient(self.server(respond)):
                pass

    def test_connection_closed(self):
        with self.assertRaisesRegex(QMPError, "closed"):
            with QMPClient(self.server(lambda stream: None)):
                pass

    def test_failed_connect_releases_lock(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            client = QMPClient(Path(folder) / "absent.sock")
            with self.assertRaises(QMPError):
                client.__enter__()
            self.assertIsNone(client.socket)
            self.assertIsNone(client.lock)


class ComputerTests(unittest.TestCase):
    def test_all_printable_ascii_is_mapped(self):
        self.assertEqual(len(text_chords("".join(chr(code) for code in range(32, 127)))), 95)
        self.assertEqual(text_chords("A!\n"), [["shift", "a"], ["shift", "1"], ["ret"]])
        self.assertEqual(text_chords("\r\n"), [["ret"]])

    def test_invalid_text_fails_before_any_input(self):
        with patch("windows_terminal.control.QMPClient") as client:
            with self.assertRaises(ValueError):
                Computer("unused").type("valid then 💥")
            client.assert_not_called()

    def test_long_typing_yields_monitor_between_batches(self):
        with patch("windows_terminal.control.QMPClient") as client, patch("windows_terminal.control.time.sleep"):
            Computer("unused").type("a" * 65, interval=0)
            self.assertEqual(client.call_count, 3)

    def test_chord_releases_reverse_order(self):
        with patch("windows_terminal.control.QMPClient") as factory:
            client = factory.return_value.__enter__.return_value
            Computer("unused").key("ctrl+alt+delete")
            presses, releases = [call.args[1]["events"] for call in client.execute.call_args_list]
            self.assertEqual([event["data"]["key"]["data"] for event in presses], ["ctrl", "alt", "delete"])
            self.assertEqual([event["data"]["key"]["data"] for event in releases], ["delete", "alt", "ctrl"])
            self.assertTrue(all(not event["data"]["down"] for event in releases))

    def test_ambiguous_key_down_failure_releases_on_fresh_connection(self):
        first, recovery = Mock(), Mock()
        first.execute.side_effect = QMPError("acknowledgement lost")
        with patch("windows_terminal.control.QMPClient") as factory:
            factory.return_value.__enter__.side_effect = [first, recovery]
            with self.assertRaisesRegex(QMPError, "acknowledgement lost"):
                Computer("unused").key("ctrl+a")
        self.assertEqual(factory.call_count, 2)
        events = recovery.execute.call_args.args[1]["events"]
        self.assertEqual([event["data"]["key"]["data"] for event in events], ["a", "ctrl"])
        self.assertTrue(all(not event["data"]["down"] for event in events))

    def test_coordinates_scale_to_absolute_range(self):
        with patch("windows_terminal.control.QMPClient") as factory:
            client = factory.return_value.__enter__.return_value
            Computer("unused").move(639, 479, 640, 480)
            events = client.execute.call_args.args[1]["events"]
            self.assertEqual([event["data"]["value"] for event in events], [32767, 32767])

    def test_out_of_bounds_and_invalid_click_are_rejected(self):
        with patch("windows_terminal.control.QMPClient") as client:
            for position in [(-1, 0), (640, 0), (0, 480)]:
                with self.assertRaises(ValueError):
                    Computer("unused").move(*position, 640, 480)
            with self.assertRaises(ValueError):
                Computer("unused").click(1, 1, "invalid")
            client.assert_not_called()

    def test_invalid_key_and_interval(self):
        for chord in ["", "win+badkey", "ctrl++", "q; rm -rf /"]:
            with self.assertRaises(ValueError):
                Computer("unused").key(chord)
        for interval in [-1, float("nan"), 2]:
            with self.assertRaises(ValueError):
                Computer("unused").type("a", interval)


class ViewerTests(unittest.TestCase):
    def test_split_arrow(self):
        decoder = InputDecoder()
        self.assertEqual(decoder.feed(b"\x1b["), [])
        self.assertEqual(decoder.feed(b"A"), [("key", "up")])

    def test_ascii_controls_and_detach(self):
        self.assertEqual(InputDecoder().feed(b"hi\x01\r\t\x7f\x1d"), [("text", "hi"), ("key", "ctrl+a"), ("key", "enter"), ("key", "tab"), ("key", "backspace"), ("detach", None)])

    def test_standalone_escape_flush(self):
        decoder = InputDecoder()
        self.assertEqual(decoder.feed(b"\x1b"), [])
        self.assertEqual(decoder.feed(b"", flush=True), [("key", "esc")])

    def test_mouse_and_paste(self):
        self.assertEqual(InputDecoder().feed(b"\x1b[<0;20;5M"), [("mouse", (0, 20, 5, True))])
        self.assertEqual(InputDecoder().feed(b"\x1b[200~hello\x1b[201~"), [("text", "hello")])

    def test_unknown_escape_is_not_typed(self):
        self.assertEqual(InputDecoder().feed(b"\x1b[99~"), [])

    def test_modified_navigation_keys(self):
        self.assertEqual(InputDecoder().feed(b"\x1b[1;5A\x1b[1;6D\x1b[3;3~"), [("key", "ctrl+up"), ("key", "shift+ctrl+left"), ("key", "alt+delete")])

    def test_modified_sequence_can_span_reads(self):
        decoder = InputDecoder()
        self.assertEqual(decoder.feed(b"\x1b[1;"), [])
        self.assertEqual(decoder.feed(b"5A"), [("key", "ctrl+up")])

    def test_paste_is_atomic_across_reads_and_timeouts(self):
        decoder = InputDecoder()
        self.assertEqual(decoder.feed(b"\x1b[200~hello\r\n"), [])
        self.assertEqual(decoder.feed(b"", flush=True), [])
        self.assertEqual(decoder.feed(b"world\x1b[20"), [])
        self.assertEqual(decoder.feed(b"1~"), [("text", "hello\r\nworld")])

    def test_paste_cannot_send_controls_or_detach(self):
        for invalid in (b"\x1d", b"\x03", b"\x1b[A", "é".encode(), b"a" * 20001):
            with self.subTest(invalid=invalid[:10]):
                self.assertEqual(InputDecoder().feed(b"\x1b[200~safe" + invalid + b"\x1b[201~"), [("paste_rejected", None)])

    def test_paste_size_is_bounded_before_terminator(self):
        decoder = InputDecoder()
        decoder.feed(b"\x1b[200~" + b"a" * 30000)
        self.assertEqual(len(decoder.paste), 0)
        self.assertLess(len(decoder.pending), len(b"\x1b[201~"))
        self.assertEqual(decoder.feed(b"\x1b[201~"), [("paste_rejected", None)])

    def test_paste_then_regular_detach(self):
        self.assertEqual(InputDecoder().feed(b"\x1b[200~hello\x1b[201~\x1d"), [("text", "hello"), ("detach", None)])

    def test_paste_markers_can_split_at_every_byte(self):
        data = b"\x1b[200~hello\tworld\r\n\x1b[201~"
        for position in range(1, len(data)):
            decoder = InputDecoder()
            actions = decoder.feed(data[:position]) + decoder.feed(data[position:])
            self.assertEqual(actions, [("text", "hello\tworld\r\n")], position)

    def test_paste_accepts_exact_limit(self):
        self.assertEqual(InputDecoder().feed(b"\x1b[200~" + b"a" * 20000 + b"\x1b[201~"), [("text", "a" * 20000)])

    def test_geometry_preserves_ratio(self):
        self.assertEqual(image_geometry(Image.new("RGB", (1920, 1080)), 100, 30), (100, 28))

    def test_blocks_and_graphics(self):
        image = Image.new("RGB", (2, 2), (10, 20, 30))
        self.assertIn("38;2;10;20;30", render(image, 1, 1, "blocks"))
        self.assertIn("a=T,f=100", render(image, 2, 2, "kitty"))
        self.assertIn("1337;File=inline=1", render(image, 2, 2, "iterm"))
        with self.assertRaises(ValueError):
            render(image, 2, 2, "unknown")

    def test_protocol_detection(self):
        for environment, expected in [({"TERM_PROGRAM": "ghostty"}, "kitty"), ({"TERM_PROGRAM": "iTerm.app"}, "iterm"), ({"TMUX": "x", "TERM_PROGRAM": "ghostty"}, "blocks"), ({}, "blocks")]:
            with patch.dict("os.environ", environment, clear=True):
                self.assertEqual(detect_protocol(), expected)


class MCPTests(unittest.TestCase):
    def request(self, method, params=None):
        return handle(Mock(), "windows", {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}})

    def test_initialize_and_tool_discovery(self):
        reply = self.request("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 9)
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in tools))

    def test_screenshot_is_native_image(self):
        with patch("windows_terminal.mcp.Computer") as computer:
            computer.return_value.png.return_value = b"png"
            result = self.request("tools/call", {"name": "screenshot"})["result"]
            self.assertEqual(base64.b64decode(result["content"][0]["data"]), b"png")
            self.assertEqual(result["content"][0]["mimeType"], "image/png")

    def test_validation_errors_are_tool_errors(self):
        for params in [{"name": "unknown"}, {"name": "click", "arguments": {"x": True, "y": 0}}, {"name": "key", "arguments": {}}, {"name": "status", "arguments": {"extra": "field"}}, {"name": "scroll", "arguments": {"direction": "down", "steps": 0}}]:
            self.assertTrue(self.request("tools/call", params)["result"]["isError"])

    def test_parse_error_and_no_notification_output(self):
        source = io.StringIO('bad json\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
        destination = io.StringIO()
        serve(Mock(), "windows", source, destination)
        replies = [json.loads(line) for line in destination.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"], {})

    def test_invalid_requests_and_methods(self):
        self.assertEqual(handle(Mock(), "windows", [1, 2])["error"]["code"], -32600)
        self.assertEqual(self.request("missing")["error"]["code"], -32601)
        self.assertEqual(handle(Mock(), "windows", {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []})["error"]["code"], -32602)
        self.assertEqual(self.request("initialize", {"protocolVersion": []})["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(handle(Mock(), "windows", {"jsonrpc": "2.0", "id": {}, "method": "ping"})["error"]["code"], -32600)

    def test_parser_supports_named_vm_and_root(self):
        args = parser().parse_args(["--root", "/tmp/test-root", "click", "12", "34", "--vm", "work"])
        self.assertEqual((args.x, args.y, args.vm), (12, 34, "work"))


if __name__ == "__main__":
    unittest.main()
