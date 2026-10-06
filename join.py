"""``python join.py <agent>`` — 把一个 agent 接进网关（薄壳，逻辑在 agent_gateway.join）。

和 ``run.cmd`` / ``login.py`` 一样，根目录的入口只是为了让命令行短一点::

    python join.py --list
    python join.py workbuddy
"""
from agent_gateway.join import main

if __name__ == "__main__":
    raise SystemExit(main())
