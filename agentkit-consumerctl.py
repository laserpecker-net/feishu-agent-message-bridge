#!/usr/bin/env python3
"""发布/注册/生效/回滚 agentkit consumer 的命令行入口（薄壳，逻辑在 agentkit.rollout）。

用法（本脚本随 release 分发，装好的机器上在 `releases/<id>/` 里直接能跑）：

  python3 agentkit-consumerctl.py status    # launchd 状态 + plist 里的 consumer 身份
  python3 agentkit-consumerctl.py register  # 只注册身份，不 claim、不加载
  python3 agentkit-consumerctl.py start     # 幂等加载（已加载则 already-loaded）
  python3 agentkit-consumerctl.py restart   # bootout+bootstrap：换 release 后必须用它
  python3 agentkit-consumerctl.py stop      # 卸载并把 plist 挪进 disabled-consumers/

回滚到上一个版本用 `feishu-bridge rollback --to <release_id>`；旧的 Cursor 引擎
（`bridge/consumer.py` / `cursor-main`）已删除，所以没有"恢复 cursor consumer"这一步。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentkit.rollout import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
