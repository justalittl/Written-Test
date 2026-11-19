"""执行命令，限制捕获的输出大小，并管理整个子进程树。"""

import ctypes
import locale
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def shell_executable():
    if os.name == "nt":
        return os.environ.get("ComSpec") or os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
    return "/bin/sh"


def available_compilers(root: Path) -> list[str]:
    """仅在 PATH 指定的目录中检查已知编译器入口，不启动进程或遍历目录。"""
    windows = os.name == "nt"
    directories = [root / (p.strip('"') if windows else p) for p in os.get_exec_path()]
    names = ["gcc", "clang", "cc", "tcc", "g++", "clang++", "c++"]
    if windows:
        names.append("cl")
    found = []
    for name in names:
        filename = name + ".exe" if windows else name
        for directory in directories:
            candidate = directory / filename
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found.append(name)
                break
    return found


class _Capture:
    def __init__(self):
        self.prefix = bytearray()
        self.size = self.newlines = 0
        self.last = b""

    def drain(self, stream):
        with stream:
            while chunk := stream.read(65536):
                self.size += len(chunk)
                self.newlines += chunk.count(b"\n")
                self.last = chunk[-1:]
                self.prefix.extend(chunk[: 6000 - len(self.prefix)])

    def render(self):
        try:
            text = self.prefix.decode("utf-8")
        except UnicodeDecodeError:
            text = self.prefix.decode(locale.getpreferredencoding(False), errors="replace")
        if self.size > len(self.prefix):
            lines = self.newlines + int(self.last != b"\n")
            text += f"\n[已截断，共 {lines} 行。可将输出重定向到工作目录文件并按行读取。]"
        return text


class _WindowsJob:
    """将等待中的启动进程加入作业对象，再允许它启动命令解释器和子进程。"""

    def __init__(self):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        try:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(self.handle)


def _wait_for_exit(process, timeout):
    if os.name != "nt":
        return process.wait(timeout=timeout)
    # Windows 的句柄等待会推迟 Python 信号处理，分段等待让主线程及时响应 Ctrl+C。
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            return process.wait(timeout=max(0, remaining))
        try:
            return process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            continue


def run(command: str, root: Path, timeout: int, expected_stdout: str | None = None) -> tuple[str, bool]:
    windows = os.name == "nt"
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("API_KEY")}
    job = _WindowsJob() if windows else None
    # Windows 启动进程收到放行字节后才会创建子进程，确保整个进程树都受作业对象管理。
    launcher = (
        "import subprocess,sys; "
        "sys.exit(subprocess.call(sys.argv[1], shell=True, executable=sys.argv[2], stdin=subprocess.DEVNULL) "
        "if sys.stdin.buffer.read(1) == b'1' else 1)"
    )
    streams = [_Capture(), _Capture()]
    workers = []
    process = None
    timed_out = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", launcher, command, shell_executable()] if windows else command,
            shell=not windows, cwd=root, env=env,
            executable=None if windows else shell_executable(),
            stdin=subprocess.PIPE if windows else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
        if job:
            job.assign(process)
            process.stdin.write(b"1")
            process.stdin.close()
        for capture, stream in zip(streams, (process.stdout, process.stderr)):
            worker = threading.Thread(target=capture.drain, args=(stream,), daemon=True)
            worker.start()
            workers.append(worker)
        try:
            _wait_for_exit(process, timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        try:
            if job:
                job.close()
            elif process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    process.kill()
                process.wait()
                for worker in workers:
                    worker.join()
                process.stdout.close()
                process.stderr.close()
    status = f"命令执行超时（{timeout} 秒）\n" if timed_out else ""
    stdout = streams[0].render()
    result = (f"{status}退出码：{process.returncode}\n标准输出：\n{stdout}"
              f"\n标准错误：\n{streams[1].render()}")
    ok = not timed_out and process.returncode == 0
    if ok and expected_stdout is not None:
        if streams[0].size > len(streams[0].prefix):
            return "标准输出已截断，无法核验预期结果。请使用断言或缩小输出范围。\n" + result, False
        actual = stdout.replace("\r\n", "\n").removesuffix("\n")
        expected = expected_stdout.replace("\r\n", "\n").removesuffix("\n")
        if actual != expected:
            return f"验证失败，预期标准输出为 {expected_stdout[:200]!r}。\n{result}", False
        result = "标准输出符合预期。\n" + result
    return result, ok
