"""兼容入口；子进程环境实现已迁至 :mod:`tricoder.process.env`。"""

from tricoder.process.env import (
    filtered_subprocess_env,
    trusted_path_executable,
    trusted_python_executable,
)

__all__ = [
    "filtered_subprocess_env",
    "trusted_path_executable",
    "trusted_python_executable",
]
