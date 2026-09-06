"""python -m dual_agent 的包内入口：默认 host 组合根 → 既有 cli.main。

双模式 import 与 cli.py 的 __version__ 先例同型（安装态包相对 /
源树平铺）。facade 的构造、注入与诚实失败语义全部住在 host_entry。
"""
try:  # installed-package mode: dual_agent.__main__
    from .host_entry import main
except ImportError:  # source-tree flat-import mode (tests/examples)
    from host_entry import main

if __name__ == "__main__":
    raise SystemExit(main())
