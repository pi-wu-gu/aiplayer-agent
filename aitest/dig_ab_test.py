# -*- coding: utf-8 -*-
"""挖方块失效根因实验：A=直接长按攻击，B=开/关背包重抓鼠标后再长按。
理论预测（基于 1.21.1 源码）：
  Minecraft.handleKeybinds() 末尾
    continueAttack(screen==null && !flag2 && keyAttack.isDown() && mouseHandler.isMouseGrabbed())
  → 鼠标未抓取时 continueAttack(false) → 每 tick stopDestroyBlock() → destroyProgress 永为 0。
  close_screen → setScreen(null) → grabMouse()（窗口激活时）→ 重抓 → B 组应成功。
"""
import os
import sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctl import cmd, call

X, Y, Z = 8, 66, 3  # looking_at 已确认对准此草方块

def block():
    return cmd({"type": "block_at", "x": X, "y": Y, "z": Z})

def hold_attack(ms):
    return cmd({"type": "keys", "keys": ["key.attack"], "duration": ms})

print("== A 组：直接长按攻击 3s ==")
b0 = block()
print("  before:", b0)
r = hold_attack(3000)
print("  hold:", r)
time.sleep(3.6)
b1 = block()
print("  after :", b1)
print("  A组结果:", "破除!" if "air" in str(b1) else "未破除(符合鼠标未抓取预测)")

print("== 重抓鼠标：press key.inventory → close_screen ==")
r = cmd({"type": "press", "key": "key.inventory"})
print("  press inventory:", r)
time.sleep(0.5)
sc = cmd({"type": "get_screen"})
print("  screen:", sc)
r = cmd({"type": "close_screen"})
print("  close:", r)
time.sleep(0.7)  # grabMouse 后需至少 1 tick 让 continueAttack(false) 把 missTime 清零

print("== B 组：重抓后再长按攻击 3s ==")
b2 = block()
print("  before:", b2)
r = hold_attack(3000)
print("  hold:", r)
time.sleep(3.6)
b3 = block()
print("  after :", b3)
print("  B组结果:", "破除!" if "air" in str(b3) else "仍未破除")

ok_a = "air" in str(b1)
ok_b = "air" in str(b3)
print("\n== 判定 ==")
if not ok_a and ok_b:
    print("★根因实锤：鼠标未抓取导致 continueAttack(false) 每 tick stopDestroyBlock；重抓后恢复")
elif ok_a and ok_b:
    print("两组都破除：当前鼠标已处于抓取状态（可能机主点过窗口），无法区分——需在确认未抓取时重测")
else:
    print("异常：A组成B组败——存在其他阻塞因素，需进一步排查")
