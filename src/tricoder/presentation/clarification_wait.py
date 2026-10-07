"""线程间传递一次性澄清结果；迟到答案不能复活已结束任务。"""

from __future__ import annotations

import threading
import time
import os
import select
import sys
from typing import TextIO

from tricoder.core.cancellation import CancellationToken
from tricoder.core.clarification import ClarificationResult


class ClarificationWait:
    """等待回答、取消、超时或宿主不可用中的第一个终态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._result: ClarificationResult | None = None

    def _resolve(self, result: ClarificationResult) -> bool:
        with self._lock:
            if self._result is not None:
                return False
            self._result = result
            self._done.set()
            return True

    def answer(self, answer: str) -> bool:
        try:
            result = ClarificationResult.answered(answer)
        except ValueError:
            return False
        return self._resolve(result)

    def cancel(self) -> bool:
        return self._resolve(ClarificationResult.cancelled())

    def timed_out(self) -> bool:
        return self._resolve(ClarificationResult.timed_out())

    def unavailable(self, reason: str = "host_unavailable") -> bool:
        return self._resolve(ClarificationResult.unavailable(reason))

    def wait(
        self,
        cancellation: CancellationToken,
        *,
        timeout: float = 300.0,
    ) -> ClarificationResult:
        if not isinstance(cancellation, CancellationToken):
            raise ValueError("澄清等待必须绑定取消令牌")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("澄清超时必须是正数")
        deadline = time.monotonic() + float(timeout)
        while True:
            if cancellation.is_cancelled:
                self.cancel()
            with self._lock:
                if self._result is not None:
                    return self._result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timed_out()
                continue
            self._done.wait(min(0.05, remaining))


def read_console_clarification(
    cancellation: CancellationToken,
    timeout: float,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> ClarificationResult:
    """从真实终端进行可取消读取；管道和自定义阻塞输入明确不可用。

    Windows 使用 ``msvcrt`` 逐字符轮询；POSIX 只在 ``select`` 表明整行可读
    后调用 ``readline``。函数不创建后台输入线程，因此超时后不会遗留读者。
    """

    source = input_stream or sys.stdin
    output = output_stream or sys.stdout
    if (
        not hasattr(source, "isatty")
        or not source.isatty()
        or timeout <= 0
    ):
        return ClarificationResult.unavailable("noninteractive")
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            return _read_windows_console(source, output, cancellation, deadline)
        return _read_posix_console(source, output, cancellation, deadline)
    except KeyboardInterrupt:
        cancellation.cancel()
        return ClarificationResult.cancelled()
    except (OSError, ValueError):
        return ClarificationResult.unavailable("host_failed")


def _read_windows_console(
    source: TextIO,
    output: TextIO,
    cancellation: CancellationToken,
    deadline: float,
) -> ClarificationResult:
    # msvcrt 只能读取进程的真实控制台；isatty 已拒绝文件和管道。
    import msvcrt

    answer: list[str] = []
    output.write("回答（可输入选项或自由文本）：")
    output.flush()
    while True:
        terminal = _terminal_state(cancellation, deadline)
        if terminal is not None:
            return terminal
        if not msvcrt.kbhit():
            time.sleep(0.03)
            continue
        character = msvcrt.getwch()
        if character == "\x03":
            cancellation.cancel()
            return ClarificationResult.cancelled()
        if character in {"\r", "\n"}:
            output.write("\n")
            output.flush()
            value = "".join(answer)
            if value.strip():
                return ClarificationResult.answered(value)
            answer.clear()
            output.write("回答不能为空，请重新输入：")
            output.flush()
            continue
        if character == "\b":
            if answer:
                answer.pop()
                output.write("\b \b")
                output.flush()
            continue
        if character in {"\x00", "\xe0"}:
            # 扩展键的第二个码不是文本答案的一部分。
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        if len(answer) < 4000 and character.isprintable():
            answer.append(character)
            output.write(character)
            output.flush()


def _read_posix_console(
    source: TextIO,
    output: TextIO,
    cancellation: CancellationToken,
    deadline: float,
) -> ClarificationResult:
    output.write("回答（可输入选项或自由文本）：")
    output.flush()
    while True:
        terminal = _terminal_state(cancellation, deadline)
        if terminal is not None:
            return terminal
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([source], [], [], min(0.05, remaining))
        if not readable:
            continue
        value = source.readline()
        if value == "":
            return ClarificationResult.unavailable("ui_closed")
        value = value.rstrip("\r\n")
        if not value.strip():
            output.write("回答不能为空，请重新输入：")
            output.flush()
            continue
        if len(value) > 4000:
            output.write("回答超过 4000 字符，请重新输入：")
            output.flush()
            continue
        return ClarificationResult.answered(value)


def _terminal_state(
    cancellation: CancellationToken,
    deadline: float,
) -> ClarificationResult | None:
    if cancellation.is_cancelled:
        return ClarificationResult.cancelled()
    if time.monotonic() >= deadline:
        return ClarificationResult.timed_out()
    return None
