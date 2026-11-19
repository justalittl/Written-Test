import argparse
import json
import os
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import load_config
from .engine import Engine
from .providers import make_provider
from .storage import Session
from .tools import Tools


def positive(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def provider_name(value):
    if value not in ("mock", "openai", "anthropic"):
        raise argparse.ArgumentTypeError("请选择 mock、openai 或 anthropic")
    return value


class ChineseHelp(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or "用法：")


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 生成的错误会包含选项名和原始输入，只翻译固定前缀。
        if message.startswith("unrecognized arguments: "):
            message = "无法识别的参数：" + message.removeprefix("unrecognized arguments: ")
        elif message.startswith("ambiguous option: "):
            option, _, choices = message.removeprefix("ambiguous option: ").partition(" could match ")
            message = f"选项 {option} 可匹配多个参数，请指定完整名称：{choices}"
        elif message.startswith("argument "):
            option, _, detail = message.removeprefix("argument ").partition(": ")
            if detail == "expected one argument":
                detail = "需要提供一个值"
            elif detail.startswith("ignored explicit argument"):
                detail = "此选项不接受参数值"
            message = f"参数 {option}：{detail}"
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n")


def options(argv):
    parser = ChineseParser(description="Mini Code Agent · 本地编程助手", add_help=False,
                           formatter_class=ChineseHelp)
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("task", metavar="任务", nargs="?", help="执行单轮任务并退出，省略则进入交互模式")
    parser.add_argument("--workspace", metavar="目录", type=Path, help="覆盖 MINI_AGENT_WORKSPACE 指定的工作目录")
    parser.add_argument("--env-file", "--config", metavar="路径", type=Path, help="指定 .env 文件，默认读取启动目录的 .env")
    parser.add_argument("--provider", type=provider_name, metavar="{mock,openai,anthropic}",
                        help="覆盖配置中的模型协议")
    parser.add_argument("--model", metavar="名称", help="覆盖配置中的模型名称")
    parser.add_argument("--base-url", metavar="地址", help="覆盖配置中的模型服务地址")
    parser.add_argument("--mock-script", metavar="路径", type=Path, help="JSON 格式的离线响应序列")
    parser.add_argument("--resume", metavar="会话编号", help="恢复指定会话")
    parser.add_argument("--max-steps", metavar="步数", type=positive, default=30, help="每轮最多调用模型的次数（默认 30）")
    parser.add_argument("--context-window", metavar="容量", type=positive, default=32768, help="上下文窗口的词元数（默认 32768）")
    parser.add_argument("--yes", action="store_true", help="自动批准写入、编辑与命令执行")
    return parser.parse_args(argv)


def build_provider(args, name, model, config=None):
    startup = name == args.provider
    settings = os.environ if config is None else config
    prefix = name.upper()
    key = (settings.get("MINI_AGENT_API_KEY") if startup else None) or settings.get(prefix + "_API_KEY")
    url = (args.base_url or settings.get("MINI_AGENT_BASE_URL")) if startup else None
    url = url or settings.get(prefix + "_BASE_URL")
    model = model or (settings.get("MINI_AGENT_MODEL") if startup else None) or settings.get(prefix + "_MODEL")
    if name != "mock" and not model:
        raise ValueError(f"请在 .env 的 {prefix}_MODEL 中填写模型名，或在启动时用 --model、"
                         f"在会话中用 /model {name} <模型名> 指定")
    script = None
    if name == "mock" and args.mock_script:
        script = json.loads(args.mock_script.read_text(encoding="utf-8-sig"))
        if not isinstance(script, list):
            raise ValueError("Mock 脚本应为 JSON 数组")
    return make_provider(name, model or "mock", key or "", url, script)


def output(chunk):
    # 转义文件、命令和模型输出中的控制字符，避免终端执行控制序列。
    sys.stdout.write("".join(c if c in "\n\t" or c.isprintable() else ascii(c)[1:-1]
                             for c in chunk))
    sys.stdout.flush()


def confirm(preview):
    output(preview + "\n")
    try:
        return input("批准此操作？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


@contextmanager
def interrupts():
    last = float("-inf")

    def interrupt(signum, frame):
        nonlocal last
        now = time.monotonic()
        if now - last < 1:
            raise SystemExit(130)
        last = now
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def interact(args, engine, session, config=None):
    while True:
        if args.task is None:
            try:
                task = input("任务> ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return 0
            if not task:
                continue
            if task in ("/exit", "/quit"):
                return 0
            if task.split()[0] == "/model":
                parts = task.split()
                if len(parts) not in (2, 3) or parts[1] not in ("openai", "anthropic", "mock"):
                    print("用法 /model <openai|anthropic|mock> [模型名]，省略模型名时使用配置中的默认值")
                    continue
                name = parts[1]
                model = parts[2] if len(parts) == 3 else None
                try:
                    engine.provider = build_provider(args, name, model, config)
                    print("当前模型", name, getattr(engine.provider, "model", model or "mock"))
                except (ValueError, OSError, RuntimeError) as exc:
                    print(session.redact(str(exc)), file=sys.stderr)
                continue
        else:
            task = args.task
        try:
            engine.turn(task)
        except KeyboardInterrupt:
            print("\n本轮已取消。")
            if args.task is not None:
                return 130
        except (ValueError, OSError, RuntimeError) as exc:
            print(session.redact(str(exc)), file=sys.stderr)
            if args.task is not None:
                return 1
        if args.task is not None:
            return 0


def main(argv=None):
    args = options(argv)
    session_id = args.resume or uuid.uuid4().hex
    keys = [v for k, v in os.environ.items() if k.upper().endswith("_API_KEY")]
    try:
        source = (args.env_file or Path(".env")).expanduser().resolve()
        config = load_config(source, required=args.env_file is not None)
        configured_home = (config or {}).get("MINI_AGENT_HOME")
        if configured_home:
            directory = source.parent / Path(configured_home).expanduser()
        else:
            directory = Path(os.getenv("MINI_AGENT_HOME") or Path.home() / ".mini-code-agent").expanduser()
        if config is not None:
            args.provider = args.provider or config.get("MINI_AGENT_PROVIDER", "mock")
            keys.extend(v for k, v in config.items() if k.upper().endswith("_API_KEY"))
        else:
            args.provider = args.provider or os.getenv("MINI_AGENT_PROVIDER", "mock")
        session = Session(directory, session_id, keys)
        with session.lock(), interrupts():
            data = session.load() if args.resume else {}
            workspace = args.workspace or data.get("root")
            if workspace is None:
                settings = os.environ if config is None else config
                configured = settings.get("MINI_AGENT_WORKSPACE")
                if configured:
                    base = Path.cwd() if config is None else source.parent
                    workspace = base / Path(configured).expanduser()
            root = Path(workspace or ".").resolve(strict=bool(args.resume))
            if not args.resume:
                try:
                    root.mkdir(parents=True, exist_ok=True)
                except FileExistsError:
                    raise ValueError(f"工作目录路径已被文件占用：{root}") from None
            if not root.is_dir() or (data and root != Path(data["root"])):
                raise ValueError("工作目录无效或与会话保存的目录不符")
            provider = build_provider(args, args.provider, args.model, config)
            tools = Tools(root, (lambda preview: True) if args.yes else confirm, config_path=source)
            engine = Engine(provider, tools, data.get("history", []),
                            lambda history: session.save(root, history), session.log, output,
                            max_steps=args.max_steps, context_window=args.context_window)
            print(f"会话 {session_id}  模型协议 {args.provider}  工作目录 {root}", flush=True)
            return interact(args, engine, session, config)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
