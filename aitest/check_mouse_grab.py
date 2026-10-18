# -*- coding: utf-8 -*-
"""直接观测鼠标抓取状态：微动物理鼠标，若 GLFW 光标已 disabled（grabbed），
MouseHandler.turnPlayer 会转动摄像机 → yaw/pitch 变化；未抓取则无任何效果。"""
import ctypes, time, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctl import state

user32 = ctypes.windll.user32

def yaw_pitch():
    s = state()
    return round(s.get("yaw", 0), 3), round(s.get("pitch", 0), 3)

y0, p0 = yaw_pitch()
print("before: yaw=%.3f pitch=%.3f" % (y0, p0))

# 微动 20px（dx）+13px（dy）：不碰按键，无点击
user32.mouse_event(0x0001, 20, 13, 0, 0)  # MOUSEEVENTF_MOVE
time.sleep(0.8)  # 等至少若干 tick 让 handleAccumulatedMovement 生效

y1, p1 = yaw_pitch()
print("after : yaw=%.3f pitch=%.3f" % (y1, p1))
dyaw = abs(y1 - y0); dpitch = abs(p1 - p0)
# yaw 可能跨 360 回绕
if dyaw > 180: dyaw = 360 - dyaw
print("变化: dyaw=%.3f dpitch=%.3f" % (dyaw, dpitch))
print()
if dyaw > 0.01 or dpitch > 0.01:
    print("→ 摄像机转动 = 鼠标处于抓取状态（grabbed）")
else:
    print("→ 摄像机无响应 = 鼠标未抓取（ungrabbed）★与 continueAttack 门禁失效的根因一致")
