import io
import json
import os
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from mini_agent.providers import ProviderError, anthropic_request, make_provider, openai_request
from mini_agent.config import load_config


def stream(*events):
    fields = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        fields.append(f"data: {data}\r\n\r\n")
    return io.BytesIO("".join(fields).encode())


def chunk(calls=None, content=None, finish=None):
    return {"choices": [{"index": 0, "delta": {"tool_calls": calls, "content": content},
                         "finish_reason": finish}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.emit = Mock()
        self.log = Mock()
        self.messages = [{"role": "system", "content": "系统指令"}, {"role": "user", "content": "你好"}]
        self.schemas = [{"name": "read_file", "description": "读取文件", "parameters": {"type": "object"}}]

    def run_provider(self, name="openai"):
        return make_provider(name, "example", "secret-key", None).complete(
            self.messages, self.schemas, self.emit, self.log)

    @patch("urllib.request.urlopen")
    def test_configured_gateway_paths(self, urlopen):
        config = load_config(Path(__file__).resolve().parents[1] / ".env.example", required=True)
        for name, path in (("openai", "/compatible-mode/v1/chat/completions"),
                           ("anthropic", "/apps/anthropic/v1/messages")):
            with self.subTest(name=name):
                urlopen.return_value = (stream(chunk(content="成功", finish="stop"), "[DONE]")
                                        if name == "openai" else stream(
                                            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                                            {"type": "message_stop"}))
                provider = make_provider(name, "test", "test-token", config[name.upper() + "_BASE_URL"])
                provider.complete(self.messages, self.schemas, self.emit, self.log)
                self.assertEqual(urlopen.call_args.args[0].full_url, "https://maas.qianwenaiapi.com" + path)

    @patch("urllib.request.urlopen")
    def test_interleaved_openai_calls_usage_and_stream(self, urlopen):
        urlopen.return_value = stream(
            chunk(content="你好"),
            chunk([{"index": 1, "id": "b", "function": {"name": "grep", "arguments": "{"}},
                   {"index": 0, "id": "a", "function": {"name": "read_file", "arguments": "{"}}]),
            chunk([{"index": 0, "function": {"arguments": '"path":"x"}'}},
                   {"index": 1, "function": {"arguments": "}"}}], finish="tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9}}, "[DONE]")
        result = self.run_provider()
        self.emit.assert_called_once_with("你好")
        self.assertEqual([c["id"] for c in result["calls"]], ["a", "b"])
        self.assertEqual(result["calls"][0]["arguments"], '{"path":"x"}')
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (40, 9))
        req = urlopen.call_args.args[0]
        self.assertEqual(json.loads(req.data)["tools"][0]["function"], self.schemas[0])
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("secret-key", str(self.log.call_args_list))

    @patch("urllib.request.urlopen")
    def test_anthropic_deltas_and_transport(self, urlopen):
        urlopen.return_value = stream(
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "成功"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "abc", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '"a"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_delta", "usage": {"output_tokens": 5}},
            {"type": "message_stop"})
        result = self.run_provider("anthropic")
        self.emit.assert_called_once_with("成功")
        self.assertEqual(result["calls"], [{"id": "abc", "name": "read_file", "arguments": '{"path":"a"}'}])
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (20, 5))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "secret-key")
        self.assertEqual(json.loads(req.data)["system"], "系统指令")

    @patch.dict(os.environ, {"no_proxy": "127.0.0.1"})
    def test_http_fixture_streams_text_before_final_chunk(self):
        emitted = Event()
        observations = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observations["path"] = self.path
                observations["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(stream(chunk(content="开头")).read())
                self.wfile.flush()
                observations["displayed_before_finish"] = emitted.wait(3)
                self.wfile.write(stream(chunk(content="结尾", finish="stop"), "[DONE]").read())

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = Thread(target=server.handle_request, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}/v1"
            result = make_provider("openai", "test", None, url).complete(
                self.messages, self.schemas, lambda _: emitted.set(), self.log)
            worker.join(3)
        self.assertEqual(result["content"], "开头结尾")
        self.assertEqual(observations["path"], "/v1/chat/completions")
        self.assertTrue(observations["body"]["stream"])
        self.assertTrue(observations["displayed_before_finish"])

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_invalid_tool_identity_is_never_committed(self, urlopen, sleep):
        def bad_stream():
            return stream(
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": 42, "name": ["read_file"], "input": {}}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"})
        urlopen.side_effect = [bad_stream() for _ in range(3)]
        with self.assertRaisesRegex(ProviderError, "工具调用缺少标识"):
            self.run_provider("anthropic")

    def test_history_conversion_groups_parallel_results_in_order(self):
        messages = self.messages + [
            {"role": "assistant", "content": "", "calls": [
                {"id": "a", "name": "read_file", "arguments": '{"path":"x"}'},
                {"id": "b", "name": "grep", "arguments": "{"}]},
            {"role": "tool", "id": "a", "content": "成功", "ok": True},
            {"role": "tool", "id": "b", "content": "JSON 无效", "ok": False}]
        body = anthropic_request(messages, self.schemas)
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual([b["tool_use_id"] for b in body["messages"][-1]["content"]], ["a", "b"])
        self.assertTrue(body["messages"][-1]["content"][1]["is_error"])
        body = openai_request(messages, self.schemas)
        self.assertEqual(body["messages"][-1]["tool_call_id"], "b")
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"path":"x"}')

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_transient_failures_back_off_and_recover(self, urlopen, sleep):
        for error in (TimeoutError(), HTTPError("url", 429, "busy", {}, io.BytesIO()),
                      HTTPError("url", 503, "busy", {}, io.BytesIO())):
            urlopen.side_effect = [error, stream(chunk(content="部分内容")), stream(chunk(content="成功", finish="stop"), "[DONE]")]
            self.assertEqual(self.run_provider()["content"], "成功")
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])
            sleep.reset_mock()

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_client_errors_fail_fast_and_redact(self, urlopen, sleep):
        for status in (400, 401):
            urlopen.side_effect = HTTPError("url", status, "error", {}, io.BytesIO(b"key secret-key invalid"))
            with self.assertRaisesRegex(ProviderError, f"HTTP {status}") as ctx:
                self.run_provider()
            self.assertNotIn("secret-key", str(ctx.exception))
            self.assertNotIn("secret-key", str(self.log.call_args_list))
        sleep.assert_not_called()

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_incomplete_call_never_reaches_agent(self, urlopen, sleep):
        partial = chunk([{"index": 0, "id": "x", "function": {"name": "write_file", "arguments": "{"}}])
        urlopen.side_effect = [stream(partial), stream(partial, "[DONE]"), stream("bogus")]
        with self.assertRaises(ProviderError):
            self.run_provider()
        self.assertEqual(urlopen.call_count, 3)

    def test_mock_script_consumption_and_argument_encoding(self):
        provider = make_provider("mock", "test", script=[
            {"calls": [{"id": "a", "name": "read_file", "arguments": {"path": "a"}}]}, {"content": "完成"}])
        first = provider.complete(self.messages, self.schemas, self.emit, self.log)
        self.assertEqual(json.loads(first["calls"][0]["arguments"]), {"path": "a"})
        self.assertEqual(provider.complete(self.messages, self.schemas, self.emit, self.log)["content"], "完成")
        with self.assertRaisesRegex(ProviderError, "回复已用完"):
            provider.complete(self.messages, self.schemas, self.emit, self.log)
