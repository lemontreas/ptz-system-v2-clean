# -*- coding: utf-8 -*-
"""
Fix: PTZ angle in Redis not updated during calibration and homing.

Changes:
1. safe_split_move: add on_move_update callback, call it in wait_axis loop
2. _goto_calibration_point: pass callback to update Redis with CALIBRATING state
3. Startup homing (PTZ_HOME_ON_START): pass callback to update Redis with HOMING state
"""

with open('ptz_control.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

content = ''.join(lines)

# ── 1. safe_split_move: add on_move_update parameter to signature ─────────────
OLD_SIG = "def safe_split_move(ptz, target_pan, target_tilt, order='pan_first', settle=None):"
NEW_SIG = "def safe_split_move(ptz, target_pan, target_tilt, order='pan_first', settle=None, on_move_update=None):"
assert OLD_SIG in content, "PATCH 1: signature not found"
content = content.replace(OLD_SIG, NEW_SIG, 1)
print("PATCH 1 OK: safe_split_move signature updated")

# ── 2. wait_axis inner loop: call on_move_update after reading position ────────
# Target the log line inside wait_axis. We inject callback call right after it.
OLD_LOG = (
    "                if current_time - last_log_time >= 1.0:\n"
    "                    logger.info(f\"\U0001f504 [{axis_name}"
)
# Try finding the line manually
log_marker = "last_log_time = current_time\n"
# Find the occurrence inside wait_axis context
idx = content.find("last_log_time = current_time\n                \n                if pan_ok and tilt_ok:")
if idx == -1:
    # Try another approach: find the marker line and inject after it
    # The line "                    last_log_time = current_time" appears in wait_axis
    # We need to find it in context. Let's look for the line preceded by "last_log_time ="
    # and followed by blank line then "if pan_ok"
    INJECTION_MARKER = "                    last_log_time = current_time\n                \n                if pan_ok and tilt_ok:"
    if INJECTION_MARKER not in content:
        # Try without extra space
        INJECTION_MARKER2 = "                    last_log_time = current_time\n\n                if pan_ok and tilt_ok:"
        assert INJECTION_MARKER2 in content, "PATCH 2: wait_axis marker not found (both variants)"
        INJECTION_MARKER = INJECTION_MARKER2

    REPLACEMENT = (
        "                    last_log_time = current_time\n"
        "                # 实时回调：通知调用方当前位置（用于更新 Redis 等外部状态）\n"
        "                if on_move_update is not None and p is not None and t is not None:\n"
        "                    try:\n"
        "                        on_move_update(p, t)\n"
        "                    except Exception:\n"
        "                        pass\n"
        "\n"
        "                if pan_ok and tilt_ok:"
    )
    content = content.replace(INJECTION_MARKER, REPLACEMENT, 1)
    print("PATCH 2 OK: on_move_update callback injected into wait_axis (variant 2)")
else:
    INJECTION_MARKER = "                    last_log_time = current_time\n                \n                if pan_ok and tilt_ok:"
    REPLACEMENT = (
        "                    last_log_time = current_time\n"
        "                # 实时回调：通知调用方当前位置（用于更新 Redis 等外部状态）\n"
        "                if on_move_update is not None and p is not None and t is not None:\n"
        "                    try:\n"
        "                        on_move_update(p, t)\n"
        "                    except Exception:\n"
        "                        pass\n"
        "                \n"
        "                if pan_ok and tilt_ok:"
    )
    content = content.replace(INJECTION_MARKER, REPLACEMENT, 1)
    print("PATCH 2 OK: on_move_update callback injected into wait_axis (variant 1)")

# ── 3. _goto_calibration_point: pass on_move_update callback ──────────────────
OLD_CAL = (
    "        if not safe_split_move(ptz, calibration_pan, calibration_tilt, \n"
    "                               order='tilt_first', settle=2.0):"
)
NEW_CAL = (
    "        def _cal_redis_update(p, t):\n"
    "            r.set(PTZ_STATUS_KEY, json.dumps({\n"
    "                \"ts\": time.time(),\n"
    "                \"position\": {\"pan\": round(p, 2), \"tilt\": round(t, 2)},\n"
    "                \"state\": \"CALIBRATING\",\n"
    "                \"calibration\": {\n"
    "                    \"active\": True,\n"
    "                    \"context\": context,\n"
    "                    \"target\": {\"pan\": 0.0, \"tilt\": 0.0}\n"
    "                }\n"
    "            }))\n"
    "        if not safe_split_move(ptz, calibration_pan, calibration_tilt,\n"
    "                               order='tilt_first', settle=2.0,\n"
    "                               on_move_update=_cal_redis_update):"
)
assert OLD_CAL in content, "PATCH 3: calibration safe_split_move call not found"
content = content.replace(OLD_CAL, NEW_CAL, 1)
print("PATCH 3 OK: calibration callback injected")

# ── 4. Startup homing: pass on_move_update callback ───────────────────────────
OLD_HOME = (
    "                if safe_split_move(ptz, home_pan, home_tilt, order='tilt_first', settle=PTZ_SPLIT_SETTLE_SEC):"
)
NEW_HOME = (
    "                def _home_redis_update(p, t):\n"
    "                    r.set(PTZ_STATUS_KEY, json.dumps({\n"
    "                        \"ts\": time.time(),\n"
    "                        \"position\": {\"pan\": round(p, 2), \"tilt\": round(t, 2)},\n"
    "                        \"state\": \"HOMING\",\n"
    "                        \"homing\": {\n"
    "                            \"active\": True,\n"
    "                            \"target\": {\"pan\": home_pan, \"tilt\": home_tilt}\n"
    "                        }\n"
    "                    }))\n"
    "                if safe_split_move(ptz, home_pan, home_tilt, order='tilt_first',\n"
    "                                   settle=PTZ_SPLIT_SETTLE_SEC,\n"
    "                                   on_move_update=_home_redis_update):"
)
assert OLD_HOME in content, "PATCH 4: homing safe_split_move call not found"
content = content.replace(OLD_HOME, NEW_HOME, 1)
print("PATCH 4 OK: homing callback injected")

with open('ptz_control.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("\nAll patches applied successfully.")
