"""以独占写锁保存会话快照，并在本地记录 JSONL 日志。"""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Session:
    def __init__(self, directory: Path, session_id: str, secrets=()):
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("会话 ID 应为启动时显示的 32 位十六进制字符串")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / (session_id + ".json")
        self.trace = directory / (session_id + ".jsonl")
        self.secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)

    @contextmanager
    def lock(self):
        with self.path.with_suffix(".lock").open("a+b") as stream:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("该会话正在另一个进程中使用") from exc
            yield

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("root"), str)
                or not isinstance(data.get("history"), list)
                or not all(isinstance(m, dict) for m in data["history"])):
            raise ValueError("会话格式错误")
        return data

    def save(self, root, history):
        # 遮盖消息正文中的密钥，保留恢复工具调用时需要的协议标识符。
        snapshot = []
        for message in history:
            item = dict(message, content=self.redact(message["content"]))
            if "calls" in message:
                item["calls"] = [dict(call, arguments=self.redact(call["arguments"]))
                                 for call in message["calls"]]
            snapshot.append(item)
        record = {"version": 1, "root": str(root), "history": snapshot}
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8",
                                             delete=False) as stream:
                name = stream.name
                json.dump(record, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if name:
                Path(name).unlink(missing_ok=True)

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[已脱敏]")
        elif isinstance(value, dict):
            return {self.redact(key): "[已脱敏]" if str(key).lower().replace("-", "_") in
                    {"authorization", "api_key", "x_api_key"} else self.redact(item)
                    for key, item in value.items()}
        elif isinstance(value, (tuple, list)):
            return [self.redact(item) for item in value]
        return value

    def log(self, event, payload):
        record = {"time": datetime.now(timezone.utc).isoformat(),
                  "event": event, "payload": self.redact(payload)}
        with self.trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
