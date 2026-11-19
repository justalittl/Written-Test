"""模型服务的流式接口适配器。只有接收完整的 SSE 消息后，才会进入工具调用循环。"""

import copy
import http.client
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    pass


class StreamError(ProviderError):
    pass


def assistant():
    return dict(role="assistant", content="", calls=[], input_tokens=0, output_tokens=0, finish="")


def openai_request(messages, schemas):
    history = []
    for m in messages:
        item = {"role": m["role"], "content": m["content"]}
        if m["role"] == "tool":
            item["tool_call_id"] = m["id"]
        if m.get("calls"):
            item["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in m["calls"]
            ]
        history.append(item)
    return {"messages": history, "tools": [{"type": "function", "function": s} for s in schemas],
            "stream_options": {"include_usage": True}}


def anthropic_request(messages, schemas):
    system = []
    history = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system.append(m["content"])
            continue
        if role == "tool":
            role = "user"
            blocks = [{"type": "tool_result", "tool_use_id": m["id"], "content": m["content"],
                       "is_error": not m.get("ok", True)}]
        else:
            blocks = [{"type": "text", "text": m["content"]}] if m["content"] else []
            for c in m.get("calls", []):
                try:
                    args = json.loads(c["arguments"])
                except ValueError:
                    args = None
                # 即使模型上一次返回的 JSON 无效，Messages API 也要求传入对象。
                if not isinstance(args, dict):
                    args = {"_invalid_arguments": c["arguments"]}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": args})
        if history and history[-1]["role"] == role:
            history[-1]["content"].extend(blocks)
        else:
            history.append({"role": role, "content": blocks})
    return {"system": "\n\n".join(system), "messages": history, "max_tokens": 4096,
            "tools": [{"name": s["name"], "description": s["description"],
                       "input_schema": s["parameters"]} for s in schemas]}


def sse_data(lines, log):
    fields = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line and fields:
            data = "\n".join(fields)
            fields.clear()
            log("response_chunk", data)
            yield data
        elif line.startswith("data:"):
            fields.append(line[5:].removeprefix(" "))
    if fields:
        log("response_partial", "\n".join(fields))
        raise StreamError("SSE 事件尚未接收完整，连接已关闭")


def add_text(result, chunk, emit):
    if chunk:
        result["content"] += chunk
        emit(chunk)


def finished(result, calls):
    if not isinstance(result["finish"], str) or not result["finish"]:
        raise StreamError("模型服务未返回停止原因")
    if any(type(result[k]) is not int or result[k] < 0 for k in ("input_tokens", "output_tokens")):
        raise StreamError("词元计数无效")
    ids = set()
    for index in sorted(calls):
        call = calls[index]
        if (not all(isinstance(call[k], str) and call[k].strip() for k in ("id", "name"))
                or call["id"] in ids):
            raise StreamError("工具调用缺少标识或名称，或标识重复")
        ids.add(call["id"])
        result["calls"].append(call)
    return result


def openai_stream(events, emit):
    result = assistant()
    calls = {}
    for data in events:
        if data == "[DONE]":
            return finished(result, calls)
        event = json.loads(data)
        if event.get("error"):
            raise StreamError("模型服务返回流式响应错误：" + str(event["error"]))
        if usage := event.get("usage"):
            result.update(input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"])
        for choice in event["choices"]:
            if choice.get("index", 0) != 0:
                continue
            delta = choice["delta"]
            add_text(result, delta.get("content") or delta.get("refusal"), emit)
            for piece in delta.get("tool_calls") or []:
                call = calls.setdefault(piece["index"], dict(id="", name="", arguments=""))
                call["id"] += piece.get("id") or ""
                function = piece.get("function") or {}
                call["name"] += function.get("name") or ""
                call["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                result["finish"] = choice["finish_reason"]
    raise StreamError("收到 [DONE] 前连接已关闭")


def anthropic_stream(events, emit):
    result = assistant()
    calls = {}
    initial = {}
    active = {}
    for data in events:
        event = json.loads(data)
        kind = event["type"]
        if kind == "message_start":
            usage = event["message"]["usage"]
            result["input_tokens"] = sum(usage.get(key, 0) for key in
                                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        elif kind == "content_block_start":
            i, block = event["index"], event["content_block"]
            if i in active or i in calls:
                raise StreamError("内容块重复")
            active[i] = block["type"]
            if block["type"] == "text":
                add_text(result, block["text"], emit)
            elif block["type"] == "tool_use":
                calls[i] = dict(id=block["id"], name=block["name"], arguments="")
                initial[i] = block["input"]
        elif kind == "content_block_delta":
            i, delta = event["index"], event["delta"]
            block_type = active[i]
            if delta["type"] == "text_delta" and block_type == "text":
                add_text(result, delta["text"], emit)
            elif delta["type"] == "input_json_delta" and block_type == "tool_use":
                calls[i]["arguments"] += delta["partial_json"]
            elif block_type == "tool_use" or delta["type"] in {"input_json_delta", "text_delta"}:
                raise StreamError("增量数据与内容块类型不匹配")
        elif kind == "content_block_stop":
            i = event["index"]
            active.pop(i)
            if i in calls and not calls[i]["arguments"]:
                calls[i]["arguments"] = json.dumps(initial[i], ensure_ascii=False)
        elif kind == "message_delta":
            reason = event.get("delta", {}).get("stop_reason")
            if reason:
                result["finish"] = reason
            if "output_tokens" in event.get("usage", {}):
                result["output_tokens"] = event["usage"]["output_tokens"]
        elif kind == "message_stop":
            if active:
                raise StreamError("连接已关闭，仍有内容块未结束")
            return finished(result, calls)
        elif kind == "error":
            raise StreamError("模型服务返回流式响应错误：" + str(event["error"]))
    raise StreamError("收到 message_stop 前连接已关闭")


class HTTPProvider:
    def __init__(self, name, model, api_key, base_url):
        self.name = name
        self.model = model
        self.api_key = api_key
        suffix = "/messages" if name == "anthropic" else "/chat/completions"
        default = "https://api.anthropic.com/v1" if name == "anthropic" else "https://api.openai.com/v1"
        url = (base_url or default).rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment or parts.username:
            raise ValueError("base_url 必须是 HTTP(S) 服务地址，且不能包含凭据、查询参数或片段标识符")
        if not parts.path or (name == "anthropic" and not url.endswith(("/v1", "/messages"))):
            url += "/v1"
        self.url = url if url.endswith(suffix) else url + suffix

    def complete(self, messages, schemas, emit, log):
        claude = self.name == "anthropic"
        body = (anthropic_request if claude else openai_request)(messages, schemas)
        if not body["tools"]:
            del body["tools"]
        body.update(model=self.model, stream=True)
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Accept": "text/event-stream"})
        if claude:
            req.add_header("anthropic-version", "2023-06-01")
        if self.api_key:
            # 凭据仅发送给原始服务，HTTP 重定向时不转发。
            req.add_unredirected_header("x-api-key" if claude else "Authorization",
                                        self.api_key if claude else "Bearer " + self.api_key)
        def safe_log(event, value):
            if self.api_key:
                value = redact(value, self.api_key)
            log(event, value)

        for attempt in range(3):
            safe_log("request", {"url": self.url, "attempt": attempt + 1, "body": body})
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    events = sse_data(response, safe_log)
                    return (anthropic_stream if claude else openai_stream)(events, emit)
            except urllib.error.HTTPError as exc:
                with exc:
                    detail = exc.read().decode(errors="replace")
                safe_log("http_error", {"status": exc.code, "body": detail})
                reason = f"HTTP {exc.code} {exc.reason}：{detail[:500]}"
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise ProviderError(redact(reason, self.api_key)) from None
            except (OSError, http.client.HTTPException, StreamError, ValueError, KeyError,
                    IndexError, TypeError, AttributeError) as exc:
                reason = f"读取模型服务的流式响应失败：{exc}"
            reason = redact(reason, self.api_key)
            safe_log("attempt_failed", {"attempt": attempt + 1, "reason": reason})
            if attempt == 2:
                raise ProviderError(reason) from None
            delay = 2 ** attempt
            emit(f"\n[将在 {delay} 秒后重新请求模型服务，第 {attempt + 1}/2 次重试]\n")
            time.sleep(delay)


def redact(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[已脱敏]")
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    return value


class MockProvider:
    def __init__(self, script=None):
        if script is not None and not isinstance(script, list):
            raise ValueError("模拟脚本必须是由助手回复组成的 JSON 数组")
        self.steps = iter(script) if script is not None else None

    def complete(self, messages, schemas, emit, log):
        log("request", {"provider": "mock", "messages": messages, "tools": schemas})
        if self.steps is None:
            if messages[-1]["role"] == "tool":
                step = {"content": "离线演示结果：\n" + messages[-1]["content"]}
            else:
                step = {"content": "离线演示：正在列出工作区文件。\n", "calls": [
                    {"id": f"demo-{len(messages)}", "name": "list_files", "arguments": {}}]}
        else:
            step = next(self.steps, None)
            if not isinstance(step, dict):
                raise ProviderError("模拟脚本中的回复已用完，或当前回复不是对象")
        result = assistant()
        result.update(copy.deepcopy(step))
        try:
            if not isinstance(result["content"], str) or not isinstance(result["calls"], list):
                raise ValueError("content 必须是字符串，calls 必须是数组")
            for call in result["calls"]:
                if not all(isinstance(call[k], str) and call[k] for k in ("id", "name")):
                    raise ValueError("工具调用的 id 和 name 必须是非空字符串")
                if not isinstance(call["arguments"], str):
                    call["arguments"] = json.dumps(call["arguments"], ensure_ascii=False)
            if any(type(result[k]) is not int or result[k] < 0 for k in ("input_tokens", "output_tokens")):
                raise ValueError("词元计数必须是非负整数")
            if result["role"] != "assistant":
                raise ValueError("模拟回复的 role 必须为 assistant")
            if len({c["id"] for c in result["calls"]}) != len(result["calls"]):
                raise ValueError("模拟回复中的工具调用 id 重复")
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(f"模拟回复无效：{exc}") from None
        result["finish"] = result["finish"] or ("tool_calls" if result["calls"] else "stop")
        for start in range(0, len(result["content"]), 16):
            emit(result["content"][start:start + 16])
        log("response", result)
        return result


def make_provider(name, model, api_key=None, base_url=None, script=None):
    if name == "mock":
        return MockProvider(script)
    if name not in {"anthropic", "openai"}:
        raise ValueError(f"未知的模型服务：{name!r}")
    return HTTPProvider(name, model, api_key, base_url)
