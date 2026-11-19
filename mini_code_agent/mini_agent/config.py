"""读取启动目录或命令行指定的 .env 文件。"""

from pathlib import Path

from dotenv.parser import parse_stream


def load_config(source: Path, *, required: bool) -> dict[str, str] | None:
    try:
        with source.open(encoding="utf-8-sig") as stream:
            values = {}
            for entry in parse_stream(stream):
                if entry.error or (entry.key and entry.value is None):
                    # 只报告行号，避免在错误信息中回显密钥。
                    raise ValueError(f"{source} 第 {entry.original.line} 行格式有误，请使用 变量名=值")
                if entry.key:
                    values[entry.key] = entry.value
            return values
    except FileNotFoundError:
        if required:
            raise ValueError(f"找不到 .env 配置文件：{source}") from None
        return None
    except UnicodeError:
        raise ValueError(f".env 文件必须采用 UTF-8 编码：{source}") from None
