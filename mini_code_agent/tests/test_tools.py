import codecs
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_agent.tools import Tools


def python_command(*args):
    argv = [sys.executable, *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class ToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.outer = Path(directory.name)
        self.root = self.outer / "work"
        self.root.mkdir()
        self.prompts = []
        self.tools = Tools(self.root, lambda text: self.prompts.append(text) is None)

    def call(self, name, **args):
        return self.tools.execute(name, json.dumps(args))

    def test_validation_rejects_invalid_json_missing_wrong_type_unknown_tool(self):
        for name, args, expected in [
            ("read_file", '{"path":', "JSON 格式无效"),
            ("missing", "{}", "未知工具：missing"),
            ("edit_file", "{}", "old_string：缺少必填字段"),
            ("read_file", '{"path":"a","start_line":"2"}', "start_line：必须是整数"),
            ("run_shell", '{"command":"x","timeout":3601}', "timeout：必须小于或等于 3600"),
            ("list_files", '{"extra":0}', "extra：不支持此字段"),
        ]:
            with self.subTest(name=name, args=args):
                message, success = self.tools.execute(name, args)
                self.assertFalse(success)
                self.assertIn(expected, message)
        self.assertEqual(6, len(self.tools.schemas))

    def test_create_numbered_read_and_edit(self):
        _, ok = self.call("write_file", path="sub/file", content="first\nsecond\n")
        self.assertTrue(ok)
        self.assertIn("+first", self.prompts[-1])
        self.assertEqual(("2: second", True), self.call("read_file", path="sub/file", start_line=2, end_line=2))
        self.assertTrue(self.call("edit_file", path="sub/file", old_string="second", new_string="third")[1])
        self.assertIn("-second", self.prompts[-1])
        self.assertEqual(b"first\nthird\n", (self.root / "sub/file").read_bytes())

    def test_denied_and_ambiguous_edits_do_not_modify(self):
        target = self.root / "file"
        target.write_bytes(b"aaa")
        for old, reason in [("aa", "多个位置"), ("unknown", "未找到")]:
            result, ok = self.call("edit_file", path="file", old_string=old, new_string="bad")
            self.assertFalse(ok)
            self.assertIn(reason, result)
        self.tools.confirm = lambda _: False
        for name, args in [
            ("write_file", dict(path="new/sub", content="x")),
            ("edit_file", dict(path="file", old_string="aaa", new_string="b")),
            ("run_shell", dict(command="echo no")),
        ]:
            self.assertIn("用户拒绝", self.call(name, **args)[0])
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(b"aaa", target.read_bytes())

    def test_preserve_bom_and_mixed_line_endings(self):
        target = self.root / "file"
        original = b"\xef\xbb\xbfalpha\r\nbravo\ncharlie"
        target.write_bytes(original)
        self.assertTrue(self.call("edit_file", path="file", old_string="bravo", new_string="B")[1])
        self.assertEqual(original.replace(b"bravo", b"B"), target.read_bytes())

    def test_edit_preserves_legacy_encoding_byte_order_and_line_endings(self):
        target = self.root / "file"
        for codec, bom, options, word in [
            ("gbk", b"", {}, "中文"),
            ("gb18030", b"", {}, "𠀀"),
            ("utf-16-le", codecs.BOM_UTF16_LE, {}, "中文"),
            ("utf-16-be", codecs.BOM_UTF16_BE, {}, "中文"),
            ("utf-32-le", codecs.BOM_UTF32_LE, {}, "中文"),
            ("utf-16-le", b"", {"encoding": "utf-16-le"}, "中文"),
            ("cp1252", b"", {"encoding": "windows-1252"}, "café"),
        ]:
            with self.subTest(codec=codec, bom=bom):
                original = "a\r\n" + word + "\nb"
                target.write_bytes(bom + original.encode(codec))
                message, ok = self.call("edit_file", path="file", old_string=word, new_string=word + "!", **options)
                self.assertTrue(ok, message)
                self.assertEqual(target.read_bytes(), bom + ("a\r\n" + word + "!\nb").encode(codec))
                self.assertIn("+" + word + "!", self.prompts[-1])

    def test_edit_rejects_encoding_errors_before_confirmation(self):
        target = self.root / "file"
        for original, old, new, options in [
            ("中文".encode("gbk"), "中文", "😀", {}),
            (codecs.BOM_UTF16_BE + "x".encode("utf-16-be"), "x", "y", {"encoding": "utf-16-le"}),
            (b"a\x00b\x00", "a", "b", {}),
            (b"plain", "plain", "new", {"encoding": "unknown-codec"}),
        ]:
            with self.subTest(original=original):
                self.prompts.clear()
                target.write_bytes(original)
                self.assertFalse(self.call("edit_file", path="file", old_string=old, new_string=new, **options)[1])
                self.assertFalse(self.prompts)
                self.assertEqual(target.read_bytes(), original)

    def test_explicit_encoding_resolves_an_ambiguous_legacy_edit(self):
        target = self.root / "file"
        original = b"\xc4\xe3 label"
        target.write_bytes(original)
        args = dict(path="file", old_string="label", new_string="中文")
        message, ok = self.call("edit_file", **args)
        self.assertFalse(ok)
        self.assertIn("encoding", message)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.call("edit_file", **args, encoding="gbk")[1])
        self.assertEqual(target.read_bytes(), b"\xc4\xe3 " + "中文".encode("gbk"))

    def test_globs_grep_and_ignored_directories(self):
        for filename in ["a.py", "src/b.py", "src/c.txt", ".git/secrets.py", "node_modules/dep.py"]:
            path = self.root / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"some\nmarker 234\n")
        result, ok = self.call("list_files", pattern="**/*.py")
        self.assertTrue(ok)
        self.assertEqual({"a.py", "src/b.py"}, set(result.splitlines()))
        self.assertEqual(("a.py", True), self.call("list_files", pattern="*.py"))
        result, ok = self.call("grep", pattern=r"marker \d{3}", glob="**/*.py")
        self.assertTrue(ok)
        self.assertEqual({"a.py:2: marker 234", "src/b.py:2: marker 234"}, set(result.splitlines()))

    def test_read_five_megabytes_and_continue(self):
        (self.root / "big").write_bytes((b"a" * 99 + b"\n") * 50000)
        result, ok = self.call("read_file", path="big")
        self.assertTrue(ok)
        self.assertLess(len(result), 12500)
        self.assertIn("50000 行", result)
        result, ok = self.call("read_file", path="big", start_line=50000)
        self.assertEqual((f"50000: {'a' * 99}", True), (result, ok))

    def test_explicit_end_line_does_not_scan_the_tail(self):
        (self.root / "log").write_bytes(b"first\nsecond\n" + b"x\n" * 50000 + b"\xff")
        self.assertEqual(("2: second", True),
                         self.call("read_file", path="log", start_line=2, end_line=2))

    def test_bounded_read_truncation_counts_requested_lines(self):
        (self.root / "log").write_bytes(b"x\n" * 1000)
        text, ok = self.call("read_file", path="log", start_line=51, end_line=300)
        self.assertTrue(ok)
        self.assertIn("250 行", text)
        self.assertIn("已截断", text)

    def test_empty_results_explain_what_was_searched(self):
        self.assertEqual(("当前目录没有可列出的条目。", True), self.call("list_files"))
        (self.root / "a.txt").write_text("example", encoding="utf-8")
        self.assertEqual(("没有文件或目录匹配该模式。", True), self.call("list_files", pattern="*.py"))
        self.assertEqual(("搜索完成，0 处匹配。", True), self.call("grep", pattern="absent"))

    def assert_file_tools_reject(self, directory, filename, reason):
        for tool, path, args in [
            ("list_files", directory, {}),
            ("grep", directory, {"pattern": "marker"}),
            ("read_file", filename, {}),
            ("write_file", filename, {"content": "changed"}),
            ("edit_file", filename, {"old_string": "marker", "new_string": "changed"}),
        ]:
            with self.subTest(tool=tool, path=path):
                message, ok = self.call(tool, path=path, **args)
                self.assertFalse(ok)
                self.assertIn(reason, message)
        self.assertEqual([], self.prompts)

    def test_workspace_paths(self):
        outside = self.outer / "outside"
        outside.mkdir()
        marker = outside / "file"
        marker.write_bytes(b"marker")
        for directory in ["../outside", r"..\outside", str(outside), str(self.root),
                          r"C:\Windows", r"C:relative", r"\\server\share"]:
            self.assert_file_tools_reject(directory, directory + "/file", "工作区内的相对路径")
        self.assertEqual(b"marker", marker.read_bytes())

    def check_external_link(self, kind):
        elsewhere = self.outer / "outside"
        elsewhere.mkdir()
        marker = elsewhere / "secret"
        marker.write_bytes(b"marker")
        link = self.root / "link"
        if kind == "junction":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
                           capture_output=True, check=True)
        else:
            try:
                link.symlink_to(marker if kind == "file" else elsewhere, target_is_directory=kind == "directory")
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("创建 Windows 符号链接需要开发者模式或相应权限")
                raise
        target = "link" if kind == "file" else "link/secret"
        self.assert_file_tools_reject("link", target, "符号链接或重解析点")
        if kind != "file":
            self.assertFalse(self.call("write_file", path="link/new", content="bad")[1])
        (self.root / "visible").write_bytes(b"marker")
        self.assertEqual(("visible", True), self.call("list_files", pattern="**/*"))
        self.assertEqual(("visible:1: marker", True), self.call("grep", pattern="marker"))
        self.assertFalse((elsewhere / "new").exists())
        self.assertEqual(b"marker", marker.read_bytes())

    def test_file_symlink_is_excluded(self):
        self.check_external_link("file")

    def test_directory_symlink_is_excluded(self):
        self.check_external_link("directory")

    @unittest.skipUnless(os.name == "nt", "目录联接仅适用于 Windows")
    def test_windows_junction_is_excluded(self):
        self.check_external_link("junction")

    def test_shell_captures_both_streams_status_and_limits(self):
        code = ("import os,sys; print('x'*50000); "
                "print(os.environ.get('OPENAI_API_KEY','removed'),file=sys.stderr); sys.exit(7)")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "hidden-value"}):
            result, ok = self.call("run_shell", command=python_command("-c", code))
        self.assertFalse(ok)
        self.assertIn("退出码：7", result)
        self.assertIn("标准错误：\nremoved", result)
        self.assertIn("已截断，共 1 行", result)
        self.assertLess(len(result), 7000)

    def test_shell_expected_output_requires_complete_stdout_and_zero_exit(self):
        for code, expected, reason in [
            ("print('a' * 6001)", "a" * 6000, "无法核验"),
            ("import sys; print(1); sys.exit(7)", "1", "退出码：7"),
            ("import sys; print(1, file=sys.stderr)", "1", "验证失败"),
            ("print('a ')", "a", "验证失败"),
        ]:
            with self.subTest(code=code):
                result, ok = self.call("run_shell", command=python_command("-c", code),
                                       expected_stdout=expected)
                self.assertFalse(ok)
                self.assertIn(reason, result)

    def test_timeout_and_interrupt_terminate_grandchild(self):
        (self.root / "child.py").write_text(
            "from pathlib import Path\nimport time\nPath('ready').touch()\n"
            "time.sleep(2)\nPath('leaked').touch()\n"
        )
        (self.root / "parent.py").write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable, 'child.py'])\ntime.sleep(10)\n"
        )
        command = python_command("parent.py")
        result, ok = self.call("run_shell", command=command, timeout=1)
        self.assertFalse(ok)
        self.assertIn("命令执行超时（1 秒）", result)
        self.assertTrue((self.root / "ready").exists())
        (self.root / "ready").unlink()
        original_wait = subprocess.Popen.wait

        def interrupt(proc, timeout=None):
            if timeout is None:
                return original_wait(proc)
            deadline = time.monotonic() + 5
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / "ready").exists())
            raise KeyboardInterrupt

        with patch.object(subprocess.Popen, "wait", interrupt), self.assertRaises(KeyboardInterrupt):
            self.call("run_shell", command=command)
        time.sleep(2.1)
        self.assertFalse((self.root / "leaked").exists())
