import multiprocessing as mp
import subprocess
import signal
import sys
import time
import os
import json
import redis
from config_loader import get_redis_config


def _start_ptz_worker():
    # 🔥 使用进程组，方便一次性杀死整个进程树
    return subprocess.Popen(
        [sys.executable, 'ptz_control.py'],
        preexec_fn=os.setsid  # 创建新的进程组
    )


def _start_capture_worker():
    return subprocess.Popen(
        [sys.executable, 'capture_worker.py'],
        preexec_fn=os.setsid  # 创建新的进程组
    )


def _restart_capture_worker_process(proc, reason):
    """只重启抓包 worker，不连带关闭 PTZ 与 Web 服务。"""
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f'[Manager] 强制停止 Capture Worker 失败: {exc}')
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
    replacement = _start_capture_worker()
    print(
        f'[Manager] Capture Worker 已单独重启: '
        f'reason={reason} old_pid={getattr(proc, "pid", None)} new_pid={replacement.pid}'
    )
    return replacement


def _start_web_server():
    return subprocess.Popen(
        [sys.executable, 'web_server.py'],
        preexec_fn=os.setsid  # 创建新的进程组
    )


def kill_existing_workers():
    """
    启动前自动清理残留的旧进程，避免端口冲突或重复运行。
    用 pkill -f 按脚本名精准匹配，排除自身（run_manager.py）。
    最后用 fuser 清掉 Web Server 占用的端口。
    """
    WORKER_SCRIPTS = [
        'ptz_control.py',
        'capture_worker.py',
        'web_server.py',
    ]
    WEB_PORT = int(os.getenv('WEB_PORT', '5000'))

    killed_any = False
    for script in WORKER_SCRIPTS:
        try:
            result = subprocess.run(
                ['pkill', '-9', '-f', script],
                capture_output=True
            )
            if result.returncode == 0:
                print(f'[Manager] 已杀掉残留进程: {script}')
                killed_any = True
        except FileNotFoundError:
            pass  # pkill 不存在（非 Linux 环境），跳过
        except Exception as e:
            print(f'[Manager] 杀进程失败 ({script}): {e}')

    # 额外清理端口占用（防止 Flask reloader 子进程残留）
    try:
        result = subprocess.run(
            ['fuser', '-k', f'{WEB_PORT}/tcp'],
            capture_output=True
        )
        if result.returncode == 0:
            print(f'[Manager] 已释放端口 {WEB_PORT}')
            killed_any = True
    except FileNotFoundError:
        pass  # fuser 不存在，跳过
    except Exception as e:
        print(f'[Manager] 释放端口失败: {e}')

    if killed_any:
        print('[Manager] 等待旧进程退出...')
        time.sleep(2)
    else:
        print('[Manager] 未发现残留进程，直接启动')


def _handle_wifi_connect_residue(r):
    """处理 wifi_connect:status 非终态残留：读取后写入错误终态。"""
    try:
        raw = r.get('wifi_connect:status')
        if not raw:
            return
        status = json.loads(raw)
        if status.get('terminal'):
            return  # 已是终态，不处理
        # 非终态：写入 error 终态
        original_state = status.get('state')
        status.update({
            'state': 'error',
            'reason': 'worker_restarted',
            'result': 'error',
            'active': False,
            'terminal': True,
        })
        r.set('wifi_connect:status', json.dumps(status))
        print(f'[Manager] wifi_connect:status 非终态已纠正为 error (原 state={original_state})')
    except Exception as e:
        print(f'[Manager] 处理 wifi_connect 残留异常（忽略）: {e}')

    # 尝试恢复 monitor 模式（失败只打日志）
    try:
        import wifi_mode_utils
        from config_loader import get_capture_config
        iface = get_capture_config()[0]
        if iface:
            wifi_mode_utils.kill_wpa_supplicant(iface)
            if wifi_mode_utils.switch_to_monitor(iface):
                print(f'[Manager] wifi_connect 残留：已恢复 {iface} 为 monitor 模式')
            else:
                print(f'[Manager] wifi_connect 残留：恢复 monitor 模式失败（忽略）')
    except Exception as e:
        print(f'[Manager] wifi_connect 残留恢复异常（忽略）: {e}')


def flush_stale_redis_state():
    """
    程序启动时清理 Redis 中可能残留的过期状态。
    防止上次异常退出/断电后，旧的 stop 标志或 status 状态阻塞新任务启动。
    不清除：历史数据、硬件配置、已识别的星链列表。
    """
    STALE_KEYS = [
        # 扫描任务状态（可能卡在 running/stopping）
        'client_scan:status',
        'client_scan:stop',
        'multi_scan:stop_initial_scan',
        'multi_scan:stop_multi_point_scan',
        'multi_scan:stop_full_area_scan',
        'full_scan:stop',
        'full_scan:active_scan_id',
        'manager:restart_capture_worker',
        'location_scan:status',
        'location_scan:stop',
        'location_scan:active_scan_id',
        'detect_channels:notify',
        # 智能扫描
        'intelligent_scan:active',
        'intelligent_scan:stop',
        'intelligent_scan:status',
        # WiFi 连接 stop/id
        'wifi_connect:stop',
        'wifi_connect:active_connect_id',
        # 命令队列（重启后旧命令无效，清掉防止被重放）
        'ptz:command_queue',
        'capture:command_queue',
    ]
    try:
        redis_host, redis_port = get_redis_config()
        r = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )
        r.ping()
        deleted = r.delete(*STALE_KEYS)
        for key in r.scan_iter(match='detect_channels:notify:*'):
            deleted += r.delete(key)

        # 处理 wifi_connect:status 非终态残留
        _handle_wifi_connect_residue(r)

        print(f'[Manager] 启动清理：已删除 {deleted} 个残留 Redis Key')
    except Exception as e:
        print(f'[Manager] 启动清理跳过（Redis 未就绪？）: {e}')


def cleanup_and_exit(procs, reason="未知原因"):
    print(f'[Manager] {reason}，准备退出主程序...')
    
    # 🔥 终止所有子进程及其子进程（使用进程组）
    for p in procs:
        try:
            if p.poll() is None:
                print(f'[Manager] 终止进程组 pgid={p.pid}')
                # 向整个进程组发送 SIGTERM
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception as e:
            print(f'[Manager] 终止进程组失败: {e}')
    
    # 等待进程组优雅退出（最多等待5秒）
    deadline = time.time() + 5
    for p in procs:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.1)
    
    # 🔥 强制杀死仍未退出的进程组
    for p in procs:
        try:
            if p.poll() is None:
                print(f'[Manager] 强制杀死进程组 pgid={p.pid}')
                # 向整个进程组发送 SIGKILL
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception as e:
            print(f'[Manager] 强制杀死进程组失败: {e}')
    
    # 最后等待一下确保清理完成
    time.sleep(0.5)
    
    print('[Manager] 主程序退出，所有子进程已清理')
    sys.exit(0)


def main():
    procs = []
    manager_redis = None
    try:
        kill_existing_workers()
        flush_stale_redis_state()

        print('[Manager] 启动 PTZ Worker...')
        procs.append(_start_ptz_worker())

        time.sleep(0.5)

        print('[Manager] 启动 Capture Worker...')
        procs.append(_start_capture_worker())

        time.sleep(0.5)

        print('[Manager] 启动 Web Server...')
        procs.append(_start_web_server())

        print('[Manager] 全部子进程已启动。按 Ctrl+C 以停止。')

        try:
            redis_host, redis_port = get_redis_config()
            manager_redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
            )
        except Exception as exc:
            print(f'[Manager] 初始化 worker 重启控制失败（继续运行）: {exc}')

        # 主进程保持存活
        while True:
            time.sleep(0.1)
            if manager_redis is not None:
                try:
                    restart_payload = manager_redis.get('manager:restart_capture_worker')
                    if restart_payload:
                        manager_redis.delete('manager:restart_capture_worker')
                        procs[1] = _restart_capture_worker_process(
                            procs[1],
                            restart_payload,
                        )
                except Exception as exc:
                    print(f'[Manager] 检查 Capture Worker 重启请求失败（忽略）: {exc}')
            # 简单健康检查：如有退出则打印
            for i,p in enumerate(procs):
                if p.poll() is not None:
                    print(f'[Manager] 子进程({i+1})退出 code={p.returncode}: pid={p.pid}')
                    print('[Manager] 主程序将退出，等待 systemd 重启服务')
                    cleanup_and_exit(procs, f"子进程{i+1}退出")

                    # 可选：决定是否重启
            
                
    except KeyboardInterrupt:
        print('\n[Manager] 收到中断信号，准备关闭...')
        cleanup_and_exit(procs, "用户中断")
    except Exception as e:
        print(f'[Manager] 发生错误: {e}')
        cleanup_and_exit(procs, f"发生错误: {e}")


if __name__ == '__main__':
    main()
