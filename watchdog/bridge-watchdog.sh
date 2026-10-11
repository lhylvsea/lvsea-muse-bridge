#!/usr/bin/env bash
# 桥接服务看门狗：检查微信/飞书/企微 serve 进程，挂了就重启
set -euo pipefail

restart_bridge() {
    local name=$1
    local dir=~/workspace/$name
    local py="python3"
    [ "$name" = "feishu-bridge" ] && py="./.venv/bin/python"
    [ "$name" = "wecom-bridge" ] && py="./.venv/bin/python"
    
    # 检查是否有 python 进程在该目录跑 bridge.py serve
    local found=0
    local pid=""
    for p in $(pgrep -f "bridge.py serve" 2>/dev/null); do
        if [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$dir" ]; then
            # 确认是 python 进程（不是 bash 包装）
            if ps -p $p -o comm= 2>/dev/null | grep -q python; then
                found=1
                pid=$p
                break
            fi
        fi
    done
    
    # 即使进程存在，也检查日志活性（无新日志视为假死）
    if [ $found -eq 1 ]; then
        local logf="$dir/serve.log"
        if [ -f "$logf" ]; then
            local mtime=$(stat -c %Y "$logf" 2>/dev/null || echo 0)
            local now=$(date +%s)
            local age=$((now - mtime))
            # 微信长轮询45秒、企微JSON心跳30秒都有日志；飞书WebSocket无定期日志，放宽到10分钟
            local threshold=300
            [ "$name" = "feishu-bridge" ] && threshold=600
            if [ $age -gt $threshold ]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') $name: 进程 $pid 存活但日志 $age 秒未更新，视为假死，重启..."
                kill -9 $pid 2>/dev/null || true
                found=0
            else
                return 0
            fi
        else
            return 0
        fi
    fi
    
    echo "$(date '+%Y-%m-%d %H:%M:%S') $name: 进程不存在，重启中..."
    cd "$dir"
    # 清理残留 pid 文件
    rm -f state/serve.pid serve.pid 2>/dev/null || true
    nohup $py bridge.py serve > serve.log 2>&1 &
    echo "$(date '+%Y-%m-%d %H:%M:%S') $name: 已重启 (pid $!)"
}

restart_bridge "wechat-bridge"
restart_bridge "feishu-bridge"
restart_bridge "wecom-bridge"
