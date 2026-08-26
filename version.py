"""版本号管理。

单一真相源：同目录下的 VERSION 文件。其他模块统一用
``from version import __version__`` 引用，避免四处硬编码版本号。
"""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


__version__ = _read_version()
version = __version__  # 别名，兼容不同引用习惯

if __name__ == "__main__":
    print(__version__)
