import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_agent.cli import build_provider, options
from mini_agent.config import load_config
from mini_agent.tools import Tools


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / ".env"

    def test_default_file_switch_and_overrides(self):
        self.path.write_text(
            "# 测试配置\nMINI_AGENT_PROVIDER=openai\nOPENAI_MODEL=first\n"
            "OPENAI_API_KEY='first#token=1' # 保留引号内的符号\n"
            "OPENAI_BASE_URL=https://first.invalid/v1\n"
            "ANTHROPIC_MODEL=second\nANTHROPIC_API_KEY=second-token\n", encoding="utf-8-sig")
        with patch.dict(os.environ, {"MINI_AGENT_MODEL": "old", "MINI_AGENT_API_KEY": "old",
                                     "ANTHROPIC_BASE_URL": "https://old.invalid"}):
            config = load_config(self.path, required=False)
            args = options(["--model", "override", "--base-url", "https://override.invalid"])
            args.provider = config["MINI_AGENT_PROVIDER"]
            first = build_provider(args, args.provider, args.model, config)
            second = build_provider(args, "anthropic", None, config)
        self.assertEqual((first.model, first.api_key), ("override", "first#token=1"))
        self.assertIn("override.invalid", first.url)
        self.assertEqual((second.model, second.api_key), ("second", "second-token"))
        self.assertIn("api.anthropic.com", second.url)

    def test_missing_explicit_and_invalid_files(self):
        self.assertIsNone(load_config(self.path, required=False))
        with self.assertRaisesRegex(ValueError, "找不到"):
            load_config(self.path, required=True)
        self.path.write_text("OPENAI_API_KEY='sensitive", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "第 1 行") as error:
            load_config(self.path, required=True)
        self.assertNotIn("sensitive", str(error.exception))

    def test_file_tools_exclude_active_dotenv(self):
        self.path.write_text("OPENAI_API_KEY=secret")
        tools = Tools(self.path.parent, lambda _: True, config_path=self.path.resolve())
        self.assertFalse(tools.execute("read_file", '{"path":".env"}')[1])
        self.assertEqual(tools.execute("grep", '{"pattern":"secret"}'), ("搜索完成，0 处匹配。", True))
