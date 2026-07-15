# wifi_mode_utils.py
# 无副作用的网卡模式切换工具，可被 capture_worker 和 run_manager 共同 import
# 任何函数都不启动独立进程、不修改全局状态、不导入 capture_worker

import subprocess
import logging
import shutil

logger = logging.getLogger(__name__)


def _run_cmd(cmd, timeout=10):
    """执行系统命令，返回 (returncode, stdout, stderr)。异常时返回 (-1, '', str(e))。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, '', f'command timeout: {cmd}'
    except FileNotFoundError:
        return -1, '', f'command not found: {cmd[0]}'
    except Exception as e:
        return -1, '', str(e)


def switch_to_managed(interface):
    """
    将指定网卡切换为 managed 模式。
    执行：ip link set <iface> down → iw <iface> set type managed → ip link set <iface> up
    返回 True 表示成功。
    """
    steps = [
        (['ip', 'link', 'set', interface, 'down'], 'down'),
        (['iw', interface, 'set', 'type', 'managed'], 'set managed'),
        (['ip', 'link', 'set', interface, 'up'], 'up'),
    ]
    for cmd, desc in steps:
        rc, out, err = _run_cmd(cmd)
        if rc != 0:
            logger.error(f"[wifi_mode] switch_to_managed {desc} 失败: rc={rc} err={err}")
            return False
    logger.info(f"[wifi_mode] {interface} 已切换为 managed 模式")
    return True


def switch_to_monitor(interface):
    """
    将指定网卡切换为 monitor 模式（仅切模式，不设信道）。
    执行：清理 dhclient → 清理路由 → ip link set <iface> down → iw <iface> set type monitor → ip link set <iface> up
    返回 True 表示成功。
    """
    # 先杀残留 wpa_supplicant 和 dhclient
    kill_wpa_supplicant(interface)
    kill_dhcp_client(interface)

    steps = [
        (['ip', 'link', 'set', interface, 'down'], 'down'),
        (['iw', interface, 'set', 'type', 'monitor'], 'set monitor'),
        (['ip', 'link', 'set', interface, 'up'], 'up'),
    ]
    for cmd, desc in steps:
        rc, out, err = _run_cmd(cmd)
        if rc != 0:
            logger.error(f"[wifi_mode] switch_to_monitor {desc} 失败: rc={rc} err={err}")
            return False
    logger.info(f"[wifi_mode] {interface} 已切换为 monitor 模式")
    return True


def kill_dhcp_client(interface=None):
    """
    杀掉残留的 DHCP 客户端进程（dhclient/udhcpc），清理路由。
    防止 WiFi 连接结束后影响以太网。
    """
    try:
        if interface:
            # 按接口杀 dhclient
            _run_cmd(['pkill', '-9', '-f', f'dhclient.*{interface}'])
            # 按接口杀 udhcpc
            _run_cmd(['pkill', '-9', '-f', f'udhcpc.*{interface}'])
            # 清理该接口的路由
            _run_cmd(['ip', 'route', 'flush', 'dev', interface])
            logger.info(f"[wifi_mode] 已清理 {interface} 的 DHCP 进程和路由")
        else:
            _run_cmd(['pkill', '-9', '-f', 'dhclient'])
            _run_cmd(['pkill', '-9', '-f', 'udhcpc'])
        # 清理可能残留的 pid/lease 文件
        _run_cmd(['rm', '-f', f'/tmp/dhclient_{interface}.pid', f'/tmp/dhclient_{interface}.leases'])
    except Exception as e:
        logger.warning(f"[wifi_mode] kill_dhcp_client 异常（忽略）: {e}")


def kill_wpa_supplicant(interface=None):
    """
    杀掉残留的 wpa_supplicant 进程（按 interface 匹配），清理 ctrl socket。
    run_manager 启动时调用，capture_worker 连接结束时也可调用。
    """
    try:
        # pkill 按 interface 参数匹配
        if interface:
            rc, out, err = _run_cmd(['pkill', '-f', f'wpa_supplicant.*{interface}'])
        else:
            rc, out, err = _run_cmd(['pkill', '-f', 'wpa_supplicant'])
        if rc == 0:
            logger.info(f"[wifi_mode] 已杀掉 wpa_supplicant 进程 (interface={interface})")
        # 清理可能残留的 ctrl socket
        if interface:
            _run_cmd(['rm', '-f', f'/var/run/wpa_supplicant/{interface}'])
    except Exception as e:
        logger.warning(f"[wifi_mode] kill_wpa_supplicant 异常（忽略）: {e}")


def check_dependencies():
    """
    检查 wpa_supplicant / wpa_cli / DHCP 客户端是否可用。
    返回 (ok, missing_list)。
    """
    missing = []
    for tool in ['wpa_supplicant', 'wpa_cli']:
        if not shutil.which(tool):
            missing.append(tool)
    # DHCP 客户端：dhclient 或 udhcpc
    if not shutil.which('dhclient') and not shutil.which('udhcpc'):
        missing.append('dhclient/udhcpc')
    return len(missing) == 0, missing
