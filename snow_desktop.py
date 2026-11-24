# -*- coding: utf-8 -*-
"""
桌面下雪 v5（Windows 10/11）
- 60 帧 pygame + numpy，雪花顺滑飘落
- 雪会落在窗口顶边上，堆成两头低、中间起伏的自然雪檐；窗口关闭、最小化、改大小或被甩动时雪会掉下来
- 鼠标划过积雪会把雪推开、溅起雪沫
- 雪量三档：小雪 / 中雪 / 大雪，托盘「雪量」切换；窗口标题栏上的雪积得更快、不会化掉
- 履带推土机：履带会转、排气管冒烟，推铲把雪顶起来往前撒开，一部分化成雪雾
- 新雪人：带明暗的三个雪球，鼠标左键点一下就会倒，三个球各自滚动、互相碰撞，帽子飞出去
- 雪人倒了以后，鼠标左键单击任意一个掉落的雪球，就会在那里重新堆起雪人
- 雪人有 6 套衣服（经典、圣诞老人、滑雪运动、国王、夏日度假、绅士），每 25 秒自动换一套，托盘「换衣服」手动换
运行：python snow_desktop.py
依赖：pip install pygame numpy pystray pillow
退出：右下角托盘的雪人图标 → 退出
说明：目前只支持主显示器
"""
import os
import sys
import math
import time
import random
import threading
import ctypes

import numpy as np

IS_WIN = sys.platform == "win32"
TEST_FRAMES = int(os.environ.get("SNOW_TEST_FRAMES", "0"))   # 测试用：非 0 时无窗口运行并截图

# ================= 可调参数 =================
FPS = 60
FLAKE_MAX = 700          # 雪花数上限（大雪时全部用上）
# 雪量档位：(同时飘落的雪花数, 每片雪花带来的积雪量)，托盘「雪量」里切换
SNOW_LEVELS = {"小雪": (260, 1.2), "中雪": (450, 2.4), "大雪": (700, 4.0)}
SNOW_DEFAULT = "中雪"
WIN_GAIN = 2.5           # 落在窗口顶上的雪额外放大，标题栏上更快积起来
COL_W = 3                # 积雪高度图每列宽度（像素）
GROUND_MAX = 70          # 地面积雪最高
WIN_MAX = 22             # 窗口顶上积雪最高（中间部分）
REPOSE = 2.0             # 安息角：相邻两列高差超过就塌
PLOW_TRIGGER = 26        # 地面平均积雪超过这个高度，铲雪车出动
PLOW_SPEED = 2.6         # 铲雪车速度（像素/帧）
PLOW_VANISH = 0.4        # 铲起来的雪有多少比例化成雪雾消失
MOUSE_R = 6              # 鼠标推雪半径（列）
WIN_SCAN_EVERY = 5       # 每几帧扫描一次窗口
FAST_MOVE = 45           # 窗口一次扫描移动超过这么多像素算「甩动」
HAT_SECONDS = 25         # 雪人站着时自动换一套衣服的间隔（秒）
GRAVITY = 0.35           # 雪球重力
KEY = (255, 0, 255)      # 透明色

# ================= Windows 接口 =================
if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def screen_info():
        r = RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), r.b
else:
    def screen_info():
        w, h = [int(v) for v in os.environ.get("SNOW_TEST_SIZE", "1280x720").split("x")]
        return w, h, h - 40

SW, SH, GROUND = screen_info()
OVERLAY = None

def get_cursor():
    if IS_WIN:
        p = POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y
    return _test_cursor()

def left_down():
    """鼠标左键是否按着（窗口鼠标穿透，直接问系统）"""
    if IS_WIN:
        return bool(user32.GetAsyncKeyState(0x01) & 0x8000)
    return _test_click()

SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

def list_windows():
    """按 Z 序（最上面在前）返回可见窗口 [(hwnd, l, t, r, b, 能否积雪)]"""
    if not IS_WIN:
        return _test_windows()
    out = []
    buf = ctypes.create_unicode_buffer(64)

    def cb(hwnd, lp):
        if hwnd == OVERLAY or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        if cloaked.value:
            return True
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value in SKIP_CLASSES:
            return True
        r = RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.r - r.l < 80 or r.b - r.t < 40:
            return True
        titled = user32.GetWindowTextLengthW(hwnd) > 0
        can = titled and 6 < r.t < GROUND - 20 and r.r > 0 and r.l < SW
        out.append((hwnd, r.l, r.t, r.r, r.b, can))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out

# ================= 积雪表面 =================
def smooth_noise(n, scale=12):
    """平滑的随机起伏，用来让积雪上限高低不平"""
    k = max(2, n // scale + 2)
    pts = np.random.uniform(0.7, 1.15, k)
    return np.interp(np.linspace(0, k - 1, n), np.arange(k), pts)

class Surface:
    def __init__(self, x0, width, base, cap, is_window, key=None):
        self.key = key
        self.x0 = x0
        self.base = base
        self.width = width
        self.n = max(1, int(width // COL_W))
        self.h = np.zeros(self.n)
        self.vis = np.ones(self.n, dtype=bool)
        self.is_window = is_window
        i = np.arange(self.n)
        if is_window:
            # 两头低中间高的雪檐形状 + 随机起伏
            edge = np.clip(np.minimum(i + 1, self.n - i) / 14.0, 0.12, 1.0)
            self.cap = cap * np.sqrt(edge) * smooth_noise(self.n)
        else:
            self.cap = cap * smooth_noise(self.n, 25)
        self.spill_l = 0.0
        self.spill_r = 0.0

    def col_x(self):
        return self.x0 + np.arange(self.n) * COL_W + COL_W * 0.5

ground = Surface(0, SW + COL_W, GROUND, GROUND_MAX, False)
win_surfs = {}
z_order = []

def relax(s, passes=2):
    """安息角塌落 + 风吹的小起伏 + 窗口边上滑落"""
    h = s.h
    for _ in range(passes):
        if s.n < 2:
            break
        d = h[:-1] - h[1:]
        f = np.where(d > REPOSE, (d - REPOSE) * 0.25,
                     np.where(d < -REPOSE, (d + REPOSE) * 0.25, 0.0))
        h[:-1] -= f
        h[1:] += f
    # 风吹：随机把一点雪挪到旁边，表面不会是死平的一条
    k = max(1, s.n // 25)
    idx = np.random.randint(0, s.n, k)
    amt = h[idx] * 0.04
    h[idx] -= amt
    np.add.at(h, np.clip(idx + np.random.choice([-2, -1, 1, 2], k), 0, s.n - 1), amt)
    if s.is_window:
        h -= 0.00004 * (h > 0)                     # 窗口上的雪极慢地化（以前化得比下得快，存不住）
    np.maximum(h, 0, out=h)
    if s.is_window:
        np.minimum(h, s.cap * 1.15, out=h)
    else:
        np.minimum(h, GROUND_MAX * 1.6, out=h)        # 地面：窗口掉下来的雪可以堆成雪包，不会被削平
    if s.is_window:
        for side in (0, -1):
            ex = h[side] - 1.0
            if ex > 0:
                h[side] -= ex
                if side == 0:
                    s.spill_l += ex
                else:
                    s.spill_r += ex
        if s.spill_l > 1.0:
            spawn(s.x0 - 3, s.base - 2, random.uniform(-1.0, -0.2), -0.3, s.spill_l)
            s.spill_l = 0
        if s.spill_r > 1.0:
            spawn(s.x0 + s.width + 3, s.base - 2, random.uniform(0.2, 1.0), -0.3, s.spill_r)
            s.spill_r = 0

# ================= 雪块 / 雪沫粒子 =================
MAXP = 20000             # 粒子池（以前 5000 太小，一扇大窗口的雪掉下来就把池子占满，旧的雪块被覆盖 = 半空消失）
P = {k: np.zeros(MAXP) for k in ("x", "y", "vx", "vy", "v", "life")}
P["alive"] = np.zeros(MAXP, dtype=bool)
_pi = [0]

def _free_slot():
    """找一个空的粒子位；池子满了才覆盖雪雾，绝不覆盖还在下落的雪块"""
    i = _pi[0]
    if P["alive"][i]:
        free = np.flatnonzero(~P["alive"])
        if len(free):
            i = int(free[0])
        else:
            mist = np.flatnonzero(P["v"] <= 0)
            i = int(mist[0]) if len(mist) else i
    _pi[0] = (i + 1) % MAXP
    return i

def spawn(x, y, vx, vy, vol, spread=0.3, life=9999, chunk=1.5):
    """受重力的雪块。vol>0 落地变积雪；vol=0 是雪雾，life 帧后消失"""
    first = True
    while vol > 0 or first:
        first = False
        part = min(vol, chunk)
        vol -= part
        i = _free_slot()
        P["x"][i] = x + random.uniform(-2, 2)
        P["y"][i] = y
        P["vx"][i] = vx + random.uniform(-spread, spread)
        P["vy"][i] = vy + random.uniform(-spread, spread)
        P["v"][i] = part
        P["life"][i] = life
        P["alive"][i] = True
        if vol <= 1e-6:
            break

def land(xs, y0, y1, vols, full=False):
    """从 y0 落到 y1 的点是否落在雪面上，落上就堆积（越接近上限越堆不上去）"""
    landed = np.zeros(len(xs), dtype=bool)
    for s in z_order + [ground]:
        col = ((xs - s.x0) // COL_W).astype(int)
        ok = (col >= 0) & (col < s.n) & ~landed
        if not ok.any():
            continue
        cc = np.clip(col, 0, s.n - 1)
        top = s.base - s.h[cc]
        if s is ground:
            hit = ok & (y1 >= top)
        else:
            hit = ok & s.vis[cc] & (y1 >= top) & (y0 <= top + 3)
        if hit.any():
            c = cc[hit]
            if full:
                # 掉下来的雪块：体积一点不丢，摊到左中右三列，像一坨雪砸下来
                v = vols[hit]
                for dc, w in ((-1, 0.25), (0, 0.5), (1, 0.25)):
                    np.add.at(s.h, np.clip(c + dc, 0, s.n - 1), v * w)
            else:
                room = np.clip(1.0 - s.h[c] / np.maximum(s.cap[c], 0.5), 0.0, 1.0)
                gain = WIN_GAIN if s.is_window else 1.0
                np.add.at(s.h, c, vols[hit] * gain * (0.15 + 0.85 * room))
            landed |= hit
    return landed

def step_particles():
    a = P["alive"]
    if not a.any():
        return
    idx = np.nonzero(a)[0]
    x, y = P["x"][idx], P["y"][idx]
    y0 = y.copy()
    vol = P["v"][idx]
    mist = vol <= 0
    P["vy"][idx] = np.minimum(P["vy"][idx] + np.where(mist, 0.06, 0.25), 9)
    P["vx"][idx] *= np.where(mist, 0.97, 0.995)
    x += P["vx"][idx]
    y += P["vy"][idx]
    P["x"][idx], P["y"][idx] = x, y
    P["life"][idx] -= 1
    falling = P["vy"][idx] > 0
    solid = falling & ~mist
    hit = np.zeros(len(idx), dtype=bool)
    if solid.any():
        hit[solid] = land(x[solid], y0[solid], y[solid], vol[solid], full=True)
    gone = hit | (y > SH) | (x < -30) | (x > SW + 30) | (P["life"][idx] <= 0)
    P["alive"][idx[gone]] = False

# ================= 雪花 =================
fx = np.random.uniform(0, SW, FLAKE_MAX)
fy = np.random.uniform(-SH, GROUND, FLAKE_MAX)
fr = np.random.choice([1, 1, 2, 2, 2, 3], FLAKE_MAX)
fvy = 0.6 + fr * 0.4 + np.random.uniform(0, 0.3, FLAKE_MAX)
fph = np.random.uniform(0, math.tau, FLAKE_MAX)
fsw = np.random.uniform(0.2, 0.7, FLAKE_MAX)
snow = {"level": SNOW_DEFAULT, "n": SNOW_LEVELS[SNOW_DEFAULT][0], "vol": SNOW_LEVELS[SNOW_DEFAULT][1]}

def set_snow(level):
    """切换雪量；新加进来的雪花从屏幕顶上开始落"""
    n, vol = SNOW_LEVELS[level]
    if n > snow["n"]:
        fy[snow["n"]:n] = np.random.uniform(-SH, -4, n - snow["n"])
    snow.update(level=level, n=n, vol=vol)

def step_flakes(t):
    n = snow["n"]
    x, y, r = fx[:n], fy[:n], fr[:n]             # 只动用当前档位的雪花（切片是原数组的视图）
    y0 = y.copy()
    y += fvy[:n]
    x += np.sin(t * 1.6 + fph[:n]) * fsw[:n] * 0.6 + 0.08
    landed = land(x, y0, y, snow["vol"] * r / 2.0)
    out = landed | (y > SH) | (x < -10) | (x > SW + 10)
    k = int(out.sum())
    if k:
        x[out] = np.random.uniform(0, SW, k)
        y[out] = np.random.uniform(-30, -4, k)

# ================= 窗口跟踪 =================
def drop_surface(s):
    """窗口关闭 / 最小化 / 甩动：上面的雪成块掉下来，落到下面的窗口或地面上堆起来"""
    xs = s.col_x()
    for c in range(0, s.n, 3):
        v = float(s.h[c:c + 3].sum())
        if v > 0.3:
            spawn(xs[c] + COL_W, s.base - s.h[c] * 0.5, random.uniform(-0.3, 0.3),
                  random.uniform(-0.6, 0.2), v, spread=0.15, chunk=4.0)
    s.h[:] = 0

def scan_windows():
    global z_order
    wins = list_windows()
    seen = set()
    new_order = []
    for i, (hwnd, l, t, r, b, can) in enumerate(wins):
        if not can:
            continue
        seen.add(hwnd)
        s = win_surfs.get(hwnd)
        if s is None or (r - l) != s.width:
            if s is not None:
                drop_surface(s)
            s = Surface(l, r - l, t, WIN_MAX, True, hwnd)
            win_surfs[hwnd] = s
        elif l != s.x0 or t != s.base:
            if abs(l - s.x0) + abs(t - s.base) > FAST_MOVE:
                drop_surface(s)
                s.h[:] = 0
            s.x0, s.base = l, t
        xs = s.col_x()
        vis = (xs >= 0) & (xs < SW)
        for (h2, l2, t2, r2, b2, _) in wins[:i]:
            if t2 <= t - 1 < b2:
                vis &= ~((xs >= l2) & (xs < r2))
        s.vis = vis
        new_order.append(s)
    for hwnd in list(win_surfs):
        if hwnd not in seen:
            drop_surface(win_surfs.pop(hwnd))
    z_order = new_order

# ================= 鼠标 =================
mouse = {"x": 0, "y": 0, "dx": 0, "dy": 0, "down": False, "click": False}

def read_mouse():
    mx, my = get_cursor()
    mouse["dx"], mouse["dy"] = mx - mouse["x"], my - mouse["y"]
    mouse["x"], mouse["y"] = mx, my
    d = left_down()
    mouse["click"] = d and not mouse["down"]
    mouse["down"] = d

def mouse_push():
    mx, my, dx = mouse["x"], mouse["y"], mouse["dx"]
    for s in z_order + [ground]:
        c = int((mx - s.x0) // COL_W)
        if c < -MOUSE_R or c >= s.n + MOUSE_R:
            continue
        if my < s.base - s.cap.max() - 8 or my > s.base + 4:
            continue
        for k in range(max(0, c - MOUSE_R), min(s.n, c + MOUSE_R + 1)):
            if not s.vis[k]:
                continue
            top = s.base - s.h[k]
            if my <= top:
                continue
            rem = min(s.h[k], (my - top) * 0.5 + 0.2)
            if rem <= 0.05:
                continue
            s.h[k] -= rem
            if abs(dx) > 1:
                dest = c + (MOUSE_R + 1) * (1 if dx > 0 else -1)
            else:
                dest = c - MOUSE_R - 1 if k < c else c + MOUSE_R + 1
            push = rem * 0.7
            if 0 <= dest < s.n:
                s.h[dest] += push
            else:
                spawn(s.x0 + dest * COL_W, s.base - 3, 0, 0, push)
            spawn(s.x0 + k * COL_W, top, dx * 0.25 + random.uniform(-0.8, 0.8),
                  random.uniform(-3.5, -1.2), rem * 0.3)
        return

# ================= 履带推土机 =================
plow = {"on": False, "x": -200.0}

def call_plow():
    if not plow["on"]:
        plow["on"] = True
        plow["x"] = -170.0

def plow_front():
    return plow["x"] + 146

def plow_step():
    """铲斗下的雪被扬起来往前上方抛洒、打散，一部分化成雪雾"""
    if not plow["on"]:
        return
    plow["x"] += PLOW_SPEED
    bf = int(plow_front() // COL_W)
    lo, hi = max(0, bf - 10), min(ground.n, bf + 1)
    if hi > lo:
        carried = float(ground.h[lo:hi].sum())
        ground.h[lo:hi] = 0
        keep = carried * (1 - PLOW_VANISH)
        fx0 = plow_front() - 4
        fy0 = GROUND - 52                                    # 铲板顶部
        while keep > 0:
            part = min(keep, random.uniform(0.4, 1.4))
            keep -= part
            sp = random.uniform(2.5, 7)                      # 推土机：雪顺着铲板翻上去再往前撒
            ang = random.uniform(math.radians(50), math.radians(85))
            spawn(fx0, fy0 + random.uniform(-10, 10), PLOW_SPEED + sp * math.cos(ang),
                  -sp * math.sin(ang), part, spread=0.8)
        for _ in range(min(12, int(carried * 0.6) + 2)):       # 雪雾
            spawn(fx0, fy0, PLOW_SPEED + random.uniform(1, 6), random.uniform(-6, -2), 0,
                  spread=1.2, life=random.randint(30, 70))
    if plow["x"] > SW + 30:
        plow["on"] = False

# ================= pygame 窗口 =================
import pygame

if TEST_FRAMES:
    os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
pygame.init()
screen = pygame.display.set_mode((SW, SH), pygame.NOFRAME)
pygame.display.set_caption("snow")

def setup_overlay():
    global OVERLAY
    if not IS_WIN or TEST_FRAMES:
        return
    hwnd = pygame.display.get_wm_info()["window"]
    OVERLAY = hwnd
    ex = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
    user32.SetLayeredWindowAttributes(hwnd, 0x00FF00FF, 0, 0x1)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

setup_overlay()

SNOW = (255, 255, 255)
SNOW_EDGE = (226, 236, 247)
SNOW_SHADE = (205, 220, 238)

def new_sprite(w, h):
    s = pygame.Surface((w, h))
    s.fill(KEY)
    s.set_colorkey(KEY)
    return s

sprites = {}
for r in (1, 2, 3, 4, 5):
    sp = new_sprite(r * 2 + 1, r * 2 + 1)
    pygame.draw.circle(sp, SNOW, (r, r), r)
    sprites[r] = sp
mist_sprite = new_sprite(3, 3)
mist_sprite.fill((236, 244, 252))

# ================= 局部刷新 =================
# 以前每帧把整块屏幕（3440x1440 约 2000 万字节）清空再整张推给 Windows，大屏会很卡。
# 现在把屏幕切成 32px 的小格，只清空、只刷新这一帧和上一帧有东西的格子。
GRID = 32
GW, GH = (SW + GRID - 1) // GRID, (SH + GRID - 1) // GRID
DIRTY = []                                   # 这一帧画过的矩形
_prev_mask = np.ones((GH, GW), dtype=bool)   # 上一帧有东西的格子（第一帧全屏）

def mark(rect):
    DIRTY.append(rect)

def _rects_to_mask(rects):
    m = np.zeros((GH, GW), dtype=bool)
    if not rects:
        return m
    a = np.array([tuple(r) for r in rects], dtype=np.int64).reshape(-1, 4)
    a = a[(a[:, 2] > 0) & (a[:, 3] > 0)]
    c0 = np.clip(a[:, 0] // GRID, 0, GW - 1); c1 = np.clip((a[:, 0] + a[:, 2]) // GRID, 0, GW - 1)
    r0 = np.clip(a[:, 1] // GRID, 0, GH - 1); r1 = np.clip((a[:, 1] + a[:, 3]) // GRID, 0, GH - 1)
    small = (c1 - c0 <= 1) & (r1 - r0 <= 1)          # 雪花这种小东西最多占 2x2 格，直接一次性标记
    for rr, cc in ((r0, c0), (r0, c1), (r1, c0), (r1, c1)):
        m[rr[small], cc[small]] = True
    for i in np.nonzero(~small)[0].tolist():         # 大的（积雪、雪人、推土机）逐个标
        m[r0[i]:r1[i] + 1, c0[i]:c1[i] + 1] = True
    return m

def _mask_runs(m):
    """把格子合并成横条矩形，减少推给 Windows 的次数"""
    pad = np.zeros((GH, GW + 2), dtype=np.int8)
    pad[:, 1:-1] = m
    d = np.diff(pad, axis=1)
    sr, sc = np.nonzero(d == 1)
    _, ec = np.nonzero(d == -1)
    return [pygame.Rect(a * GRID, r * GRID, (b - a) * GRID, GRID)
            for r, a, b in zip(sr.tolist(), sc.tolist(), ec.tolist())]

def draw_surface(s):
    """画积雪：顶面平滑处理，底部一条浅蓝阴影，看起来有厚度"""
    show = s.vis & (s.h > 0.4)
    if not show.any():
        return
    hs = np.convolve(s.h, np.array([1, 2, 3, 2, 1]) / 9.0, mode="same")
    xs = s.x0 + np.arange(s.n) * COL_W
    tops = s.base - hs
    idx = np.nonzero(show)[0]
    breaks = np.nonzero(np.diff(idx) > 1)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [len(idx) - 1]))
    for a, b in zip(starts, ends):
        i0, i1 = idx[a], idx[b]
        top_pts = list(zip((xs[i0:i1 + 1] + COL_W * 0.5).tolist(), tops[i0:i1 + 1].tolist()))
        pts = [(xs[i0], s.base + 1)] + top_pts + [(xs[i1] + COL_W, s.base + 1)]
        mark(pygame.draw.polygon(screen, SNOW, pts))
        # 底部阴影：只在雪厚的地方画
        shade = [(x, max(y, s.base - 3)) for x, y in top_pts]
        pygame.draw.polygon(screen, SNOW_SHADE, [(xs[i0], s.base + 1)] + shade + [(xs[i1] + COL_W, s.base + 1)])
        if len(top_pts) > 1:
            pygame.draw.lines(screen, SNOW_EDGE, False, top_pts, 1)

# ================= 雪人（雪球物理）=================
def shaded_ball(sp, c, r):
    """画一个有明暗的雪球：外圈偏蓝，往左上越来越亮"""
    steps = [(196, 212, 232), (214, 227, 243), (228, 237, 248), (240, 246, 252), (250, 252, 255), (255, 255, 255)]
    for i, col in enumerate(steps):
        rr = r - i * r * 0.11
        off = i * r * 0.07
        pygame.draw.circle(sp, col, (int(c - off), int(c - off)), int(rr))

def coal(sp, x, y, r):
    pygame.draw.circle(sp, (28, 28, 34), (int(x), int(y)), r)
    pygame.draw.circle(sp, (120, 120, 135), (int(x - r * 0.35), int(y - r * 0.35)), max(1, r // 3))

# ================= 雪人装扮 =================
# 每套：名字、帽子编号、围巾/领子样式、纽扣、手套、脸上配饰
OUTFITS = [
    {"name": "经典",     "hat": 0, "neck": "scarf_red",  "btn": "coal", "mitt": None,            "face": None},
    {"name": "圣诞老人", "hat": 1, "neck": "santa",      "btn": "red",  "mitt": (40, 40, 44),    "face": "beard"},
    {"name": "滑雪运动", "hat": 2, "neck": "scarf_blue", "btn": "coal", "mitt": (230, 60, 60),   "face": "goggles"},
    {"name": "国王",     "hat": 3, "neck": "cape",       "btn": "gold", "mitt": (250, 250, 252), "face": None},
    {"name": "夏日度假", "hat": 4, "neck": "lei",        "btn": "coal", "mitt": None,            "face": "sunglasses"},
    {"name": "绅士",     "hat": 0, "neck": "bowtie",     "btn": "coal", "mitt": (245, 245, 245), "face": "monocle"},
]
outfit = {"i": 0}

def draw_neck(sp, c, r, style):
    """在中间雪球顶部画围巾 / 领子"""
    dc = pygame.draw
    ny = c - r                      # 脖子位置
    if style in ("scarf_red", "scarf_blue"):
        if style == "scarf_red":
            main, dark, stripe = (206, 44, 52), (160, 28, 38), (250, 236, 210)
        else:
            main, dark, stripe = (40, 110, 210), (24, 70, 150), (250, 250, 255)
        dc.ellipse(sp, main, (c - r * 0.8, ny - 6, r * 1.6, 14))
        for k in range(4):
            xx = c - r * 0.6 + k * r * 0.4
            dc.line(sp, stripe, (xx, ny - 4), (xx + 4, ny + 6), 2)
        tail = [(c + r * 0.25, ny + 2), (c + r * 0.55, ny + 2), (c + r * 0.65, ny + 30), (c + r * 0.32, ny + 32)]
        dc.polygon(sp, main, tail)
        dc.line(sp, dark, tail[1], tail[2], 2)
        for k in range(4):
            fx_ = c + r * 0.34 + k * 4
            dc.line(sp, main, (fx_, ny + 31), (fx_, ny + 37), 2)
    elif style == "santa":          # 白毛领 + 黑腰带金扣
        for k in range(7):
            dc.circle(sp, (250, 250, 252), (int(c - r * 0.7 + k * r * 0.233), int(ny + 1)), 6)
        dc.rect(sp, (30, 30, 34), (c - r * 0.97, c + r * 0.22, r * 1.94, 8))
        dc.rect(sp, (240, 190, 40), (c - 7, c + r * 0.22 - 2, 14, 12), 3)
    elif style == "cape":           # 紫色披风领 + 白色貂皮点点
        dc.polygon(sp, (110, 40, 150), [(c - r * 1.05, ny + 14), (c - r * 0.6, ny - 6), (c + r * 0.6, ny - 6), (c + r * 1.05, ny + 14), (c, ny + 8)])
        dc.ellipse(sp, (250, 250, 252), (c - r * 0.75, ny - 8, r * 1.5, 11))
        for k in range(5):
            dc.circle(sp, (20, 20, 24), (int(c - r * 0.5 + k * r * 0.25), int(ny - 3)), 1)
    elif style == "lei":            # 夏威夷花环
        cols = [(255, 90, 140), (255, 200, 40), (255, 130, 40), (180, 90, 230), (60, 200, 140)]
        for k in range(11):
            a = math.radians(200 + k * 14)
            dc.circle(sp, cols[k % 5], (int(c + math.cos(a) * r * 0.75), int(ny + 6 - math.sin(a) * 9)), 4)
    elif style == "bowtie":         # 黑领结
        dc.polygon(sp, (24, 24, 28), [(c, ny + 4), (c - 14, ny - 4), (c - 14, ny + 12)])
        dc.polygon(sp, (24, 24, 28), [(c, ny + 4), (c + 14, ny - 4), (c + 14, ny + 12)])
        dc.circle(sp, (60, 60, 70), (int(c), int(ny + 4)), 4)

def draw_buttons(sp, c, r, style):
    for k in range(3):
        y = c - r * 0.25 + k * r * 0.33
        if style == "red":
            pygame.draw.circle(sp, (210, 40, 50), (int(c), int(y)), 4)
        elif style == "gold":
            pygame.draw.circle(sp, (240, 196, 40), (int(c), int(y)), 4)
            pygame.draw.circle(sp, (255, 240, 170), (int(c - 1), int(y - 1)), 1)
        else:
            coal(sp, c, y, 4)

def draw_face_extra(sp, c, r, style):
    dc = pygame.draw
    ex, ey = r * 0.36, c - r * 0.22
    if style == "beard":            # 白胡子盖住嘴
        for k in range(9):
            a = math.radians(20 + k * 17.5)
            dc.circle(sp, (250, 250, 252), (int(c + math.cos(a) * r * 0.62), int(c + r * 0.2 + math.sin(a) * r * 0.55)), 7)
        dc.circle(sp, (250, 250, 252), (int(c), int(c + r * 0.85)), 8)
    elif style == "goggles":        # 滑雪镜
        dc.line(sp, (40, 40, 44), (c - r * 0.95, ey), (c + r * 0.95, ey), 3)
        dc.ellipse(sp, (255, 150, 30), (c - ex - 9, ey - 7, 18 + ex * 2, 14))
        dc.ellipse(sp, (60, 60, 64), (c - ex - 9, ey - 7, 18 + ex * 2, 14), 2)
        dc.line(sp, (255, 230, 170), (c - ex - 3, ey - 3), (c - ex + 4, ey - 3), 2)
    elif style == "sunglasses":     # 墨镜
        for sx in (-1, 1):
            dc.ellipse(sp, (20, 20, 24), (c + sx * ex - 7, ey - 5, 14, 11))
        dc.line(sp, (20, 20, 24), (c - ex + 6, ey - 2), (c + ex - 6, ey - 2), 2)
    elif style == "monocle":        # 单片眼镜
        dc.circle(sp, (200, 170, 60), (int(c + ex), int(ey)), 8, 2)
        dc.line(sp, (200, 170, 60), (c + ex + 5, ey + 6), (c + ex + 9, ey + 22), 1)

def make_ball(kind, r):
    o = OUTFITS[outfit["i"]]
    pad = 46 if kind == "mid" else (30 if kind == "head" else 4)
    S = 2 * (r + pad)
    sp = new_sprite(S, S)
    c = S // 2
    if kind == "mid":       # 树枝手臂先画，球盖在上面
        for sgn in (-1, 1):
            x0, y0 = c + sgn * r * 0.7, c - r * 0.15
            x1, y1 = c + sgn * (r + 40), c - r * 0.95
            pygame.draw.line(sp, (98, 64, 36), (x0, y0), (x1, y1), 4)
            mx, my = x0 + (x1 - x0) * 0.65, y0 + (y1 - y0) * 0.65
            pygame.draw.line(sp, (98, 64, 36), (mx, my), (mx + sgn * 8, my - 13), 3)
            pygame.draw.line(sp, (98, 64, 36), (x1, y1), (x1 + sgn * 9, y1 - 3), 2)
            pygame.draw.line(sp, (98, 64, 36), (x1, y1), (x1 + sgn * 3, y1 - 10), 2)
            if o["mitt"]:                   # 手套
                pygame.draw.circle(sp, o["mitt"], (int(x1), int(y1)), 7)
                pygame.draw.circle(sp, o["mitt"], (int(x1 - sgn * 5), int(y1 + 3)), 4)
    shaded_ball(sp, c, r)
    if kind == "bottom":
        for (dx, dy) in ((-0.35, -0.2), (0.3, 0.25), (-0.1, 0.45)):
            pygame.draw.circle(sp, (238, 244, 251), (int(c + dx * r), int(c + dy * r)), 3)
    elif kind == "mid":
        draw_buttons(sp, c, r, o["btn"])
        draw_neck(sp, c, r, o["neck"])
    elif kind == "head":
        coal(sp, c - r * 0.36, c - r * 0.22, 4)
        coal(sp, c + r * 0.36, c - r * 0.22, 4)
        pygame.draw.circle(sp, (255, 190, 200), (int(c - r * 0.62), int(c + r * 0.2)), 4)
        pygame.draw.circle(sp, (255, 190, 200), (int(c + r * 0.62), int(c + r * 0.2)), 4)
        nose = [(c - 1, c - 3), (c + r + 8, c + 1), (c - 1, c + 5)]
        pygame.draw.polygon(sp, (255, 132, 24), nose)
        for k in (0.35, 0.6, 0.85):
            xx = c + (r + 8) * k
            pygame.draw.line(sp, (205, 95, 10), (xx, c - 2 + k * 3), (xx, c + 4 - k * 2), 1)
        for k in range(5):
            a = math.radians(200 + k * 35)
            coal(sp, c + math.cos(a) * r * 0.5 * -1, c + r * 0.38 + abs(k - 2) * -2, 2)
        draw_face_extra(sp, c, r, o["face"])
    return sp

def make_hat(style):
    """帽子精灵，帽檐底部中心在 (45, 52)"""
    sp = new_sprite(90, 70)
    x, top = 45, 52
    dc = pygame.draw
    if style == 0:      # 礼帽
        dc.ellipse(sp, (20, 20, 24), (x - 28, top - 6, 56, 10))
        dc.rect(sp, (24, 24, 28), (x - 16, top - 38, 32, 35))
        dc.rect(sp, (190, 40, 50), (x - 16, top - 12, 32, 6))
        dc.line(sp, (70, 70, 80), (x - 12, top - 36), (x - 12, top - 14), 2)
    elif style == 1:    # 圣诞帽
        dc.polygon(sp, (205, 35, 45), [(x - 22, top - 2), (x + 22, top - 2), (x + 8, top - 30), (x + 32, top - 38)])
        dc.rect(sp, (250, 250, 252), (x - 24, top - 8, 48, 10), border_radius=4)
        dc.circle(sp, (250, 250, 252), (x + 32, top - 38), 7)
    elif style == 2:    # 毛线帽
        dc.ellipse(sp, (46, 120, 210), (x - 22, top - 30, 44, 40))
        for k in range(5):
            dc.line(sp, (80, 150, 230), (x - 14 + k * 7, top - 24), (x - 14 + k * 7, top - 6), 2)
        dc.rect(sp, (245, 200, 60), (x - 23, top - 8, 46, 10), border_radius=3)
        dc.circle(sp, (245, 200, 60), (x, top - 32), 8)
    elif style == 3:    # 皇冠
        dc.polygon(sp, (242, 196, 30), [(x - 20, top), (x + 20, top), (x + 23, top - 24), (x + 11, top - 11),
                                        (x, top - 28), (x - 11, top - 11), (x - 23, top - 24)])
        for dx, col in ((-11, (230, 70, 60)), (0, (50, 140, 220)), (11, (40, 190, 110))):
            dc.circle(sp, col, (x + dx, top - 5), 3)
    else:               # 草帽
        dc.ellipse(sp, (220, 182, 104), (x - 36, top - 8, 72, 13))
        dc.ellipse(sp, (232, 196, 118), (x - 17, top - 26, 34, 26))
        dc.rect(sp, (60, 120, 200), (x - 16, top - 9, 32, 4))
    return sp

HAT_SPRITES = [make_hat(i) for i in range(5)]

class Ball:
    def __init__(self, kind, r):
        self.kind, self.r = kind, r
        self.m = r * r
        self.sprite = make_ball(kind, r)
        self.x = self.y = 0.0
        self.vx = self.vy = 0.0
        self.ang = 0.0          # 角度（度）
        self.w = 0.0            # 空中自转

balls = [Ball("bottom", 36), Ball("mid", 27), Ball("head", 20)]

def set_outfit(i):
    """换一整套衣服：重画雪球贴图 + 换帽子"""
    outfit["i"] = i % len(OUTFITS)
    for b in balls:
        b.sprite = make_ball(b.kind, b.r)
    hat["style"] = OUTFITS[outfit["i"]]["hat"]
    hat["t"] = time.time()

hat = {"style": 0, "x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0, "ang": 0.0, "w": 0.0, "t": time.time()}
sm = {"state": "stand", "bx": SW - 130, "build": 0, "from": None}

def floor_at(x):
    c = int(min(ground.n - 1, max(0, x // COL_W)))
    return GROUND - ground.h[c] * 0.55          # 球会陷进雪里一点

def stack_targets(bx):
    fy = floor_at(bx)
    b, m, h = balls
    yb = fy - b.r
    ym = yb - b.r - m.r + 9
    yh = ym - m.r - h.r + 7
    return [(bx, yb), (bx, ym), (bx, yh)], (bx, yh - h.r + 4 - 17)   # 帽子精灵中心

def knock(direction):
    """被撞倒：上面的球飞得更远，帽子飞出去"""
    sgn = 1 if direction >= 0 else -1
    sm["state"] = "loose"
    b, m, h = balls
    b.vx, b.vy = sgn * 0.6, 0
    m.vx, m.vy, m.w = sgn * 2.6, -1.5, -sgn * 4
    h.vx, h.vy, h.w = sgn * 4.2, -3.0, -sgn * 7
    hat.update(vx=sgn * 4.5 + random.uniform(-1, 1), vy=-6, w=-sgn * random.uniform(6, 12))

def rebuild(bx):
    bx = min(SW - 80, max(80, bx))
    sm["state"] = "build"
    sm["bx"] = bx
    sm["build"] = 0
    sm["from"] = [(b.x, b.y, ((b.ang + 180) % 360) - 180) for b in balls] + [(hat["x"], hat["y"], ((hat["ang"] + 180) % 360) - 180)]

def step_snowman():
    b, m, h = balls
    if sm["state"] == "stand":
        tg, hp = stack_targets(sm["bx"])
        for ball, (x, y) in zip(balls, tg):
            ball.x, ball.y, ball.vx, ball.vy, ball.ang = x, y, 0, 0, 0
        hat["x"], hat["y"], hat["ang"] = hp[0], hp[1], 0
        if time.time() - hat["t"] > HAT_SECONDS:
            set_outfit(outfit["i"] + 1)
        # 鼠标左键点一下雪人就把它推倒：点左边往右倒，点右边往左倒
        if mouse["click"]:
            for ball in balls:
                if math.hypot(mouse["x"] - ball.x, mouse["y"] - ball.y) < ball.r + 6:
                    knock(ball.x - mouse["x"] if abs(ball.x - mouse["x"]) > 2 else random.choice((-1, 1)))
                    return
        # 铲雪车开过来也会撞倒
        if plow["on"] and plow_front() > b.x - b.r and plow["x"] < b.x:
            knock(1)
        return

    if sm["state"] == "build":
        sm["build"] += 1
        t = min(1.0, sm["build"] / 45.0)
        e = t * t * (3 - 2 * t)
        tg, hp = stack_targets(sm["bx"])
        for i, ball in enumerate(balls):
            x0, y0, a0 = sm["from"][i]
            ti = min(1.0, max(0.0, (t - i * 0.12) / 0.76))
            ei = ti * ti * (3 - 2 * ti)
            ball.x = x0 + (tg[i][0] - x0) * ei
            ball.y = y0 + (tg[i][1] - y0) * ei - math.sin(math.pi * ei) * 60
            ball.ang = a0 * (1 - ei)
        x0, y0, a0 = sm["from"][3]
        hat["x"] = x0 + (hp[0] - x0) * e
        hat["y"] = y0 + (hp[1] - y0) * e - math.sin(math.pi * e) * 90
        hat["ang"] = a0 * (1 - e)
        if t >= 1.0:
            sm["state"] = "stand"
            hat["t"] = time.time()
        return

    # ---- loose：三个球自由滚动 ----
    # 单击掉落的雪球（或帽子）就地重新堆雪人（先判断点击，再处理推球）
    if mouse["click"]:
        for ball in balls:
            if math.hypot(mouse["x"] - ball.x, mouse["y"] - ball.y) < ball.r + 6:
                rebuild(ball.x)
                return
        if math.hypot(mouse["x"] - hat["x"], mouse["y"] - hat["y"]) < 24:
            rebuild(hat["x"])
            return
    for ball in balls:
        ball.vy += GRAVITY
        ball.vx *= 0.997
        ball.x += ball.vx
        ball.y += ball.vy
        fl = floor_at(ball.x)
        if ball.y + ball.r >= fl:
            ball.y = fl - ball.r
            ball.vy = -ball.vy * 0.25 if ball.vy > 2.5 else 0
            ball.vx *= 0.985                           # 在雪里滚的阻力
            ball.ang -= ball.vx / ball.r * 57.3        # 纯滚动
            ball.w = 0
        else:
            ball.ang += ball.w
            ball.w *= 0.99
        if ball.x < ball.r:
            ball.x, ball.vx = ball.r, abs(ball.vx) * 0.4
        if ball.x > SW - ball.r:
            ball.x, ball.vx = SW - ball.r, -abs(ball.vx) * 0.4
        if ball.y < ball.r:
            ball.y, ball.vy = ball.r, abs(ball.vy) * 0.3
        # 铲雪车推球
        if plow["on"] and plow["x"] + 100 < ball.x < plow_front() + ball.r and ball.y > GROUND - 90:
            ball.x = plow_front() + ball.r
            ball.vx = max(ball.vx, PLOW_SPEED + 0.5)
    # 球与球碰撞
    for _ in range(4):
        for i in range(3):
            for j in range(i + 1, 3):
                A, B = balls[i], balls[j]
                dx, dy = B.x - A.x, B.y - A.y
                d = math.hypot(dx, dy)
                if 0.01 < d < A.r + B.r:
                    nx, ny = dx / d, dy / d
                    ov = A.r + B.r - d
                    ia, ib = 1 / A.m, 1 / B.m
                    A.x -= nx * ov * ia / (ia + ib)
                    A.y -= ny * ov * ia / (ia + ib)
                    B.x += nx * ov * ib / (ia + ib)
                    B.y += ny * ov * ib / (ia + ib)
                    vrel = (B.vx - A.vx) * nx + (B.vy - A.vy) * ny
                    if vrel < 0:
                        jimp = -(1.15) * vrel / (ia + ib)
                        A.vx -= jimp * ia * nx
                        A.vy -= jimp * ia * ny
                        B.vx += jimp * ib * nx
                        B.vy += jimp * ib * ny
    # 帽子
    hat["vy"] += GRAVITY
    hat["vx"] *= 0.995
    hat["x"] += hat["vx"]
    hat["y"] += hat["vy"]
    hat["ang"] += hat["w"]
    fl = floor_at(hat["x"])
    if hat["y"] + 12 >= fl:
        hat["y"] = fl - 12
        hat["vy"] = -hat["vy"] * 0.2 if hat["vy"] > 2 else 0
        hat["vx"] *= 0.9
        hat["w"] *= 0.8
        rest = round(hat["ang"] / 90.0) * 90.0              # 慢慢躺平
        hat["ang"] += (rest - hat["ang"]) * 0.15
    hat["x"] = min(SW - 20, max(20, hat["x"]))

def draw_snowman():
    for ball in balls:
        spr = pygame.transform.rotate(ball.sprite, ball.ang) if abs(ball.ang) > 0.5 else ball.sprite
        mark(screen.blit(spr, spr.get_rect(center=(int(ball.x), int(ball.y)))))
    spr = HAT_SPRITES[hat["style"]]
    if abs(hat["ang"]) > 0.5:
        spr = pygame.transform.rotate(spr, hat["ang"])
    mark(screen.blit(spr, spr.get_rect(center=(int(hat["x"]), int(hat["y"])))))

# ================= 履带推土机外观 =================
def draw_plow():
    if not plow["on"]:
        return
    x, g = int(plow["x"]), GROUND
    mark((x - 40, g - 170, 300, 175))                   # 推土机整体范围
    dc = pygame.draw
    Y, YD, DK = (245, 180, 20), (170, 115, 0), (40, 40, 44)
    # 履带：圆角长条 + 会动的履带齿
    tx0, tx1, ty0, ty1 = x + 4, x + 112, g - 26, g - 1
    dc.rect(screen, DK, (tx0, ty0, tx1 - tx0, ty1 - ty0), border_radius=12)
    off = int(plow["x"] * 1.0) % 10
    for px in range(tx0 + 8 - off, tx1 - 6, 10):        # 上下两排履带齿
        dc.line(screen, (90, 90, 96), (px, ty0 + 1), (px, ty0 + 5), 3)
        dc.line(screen, (90, 90, 96), (px, ty1 - 5), (px, ty1 - 1), 3)
    for wx in (tx0 + 12, tx1 - 12):                     # 驱动轮 / 引导轮
        dc.circle(screen, (70, 70, 76), (wx, g - 13), 10)
        dc.circle(screen, (150, 150, 156), (wx, g - 13), 4)
    for wx in range(tx0 + 30, tx1 - 24, 17):            # 负重轮
        dc.circle(screen, (100, 100, 106), (wx, g - 11), 6)
    # 机身
    dc.rect(screen, Y, (x + 6, g - 50, 102, 24), border_radius=3)
    dc.rect(screen, YD, (x + 6, g - 50, 102, 24), 2, border_radius=3)
    for k in range(5):                                  # 发动机散热格栅
        dc.line(screen, YD, (x + 70 + k * 6, g - 46), (x + 70 + k * 6, g - 32), 2)
    # 驾驶室
    dc.rect(screen, Y, (x + 14, g - 88, 46, 40), border_radius=3)
    dc.rect(screen, YD, (x + 14, g - 88, 46, 40), 2, border_radius=3)
    dc.rect(screen, (174, 227, 255), (x + 20, g - 82, 34, 20))
    dc.line(screen, (230, 248, 255), (x + 24, g - 80), (x + 32, g - 66), 2)
    dc.rect(screen, YD, (x + 10, g - 92, 54, 6))         # 顶棚
    blink = (231, 76, 60) if (pygame.time.get_ticks() // 300) % 2 else (255, 200, 60)
    dc.rect(screen, blink, (x + 32, g - 99, 10, 7))
    # 排气管 + 黑烟
    dc.rect(screen, DK, (x + 84, g - 72, 6, 22))
    t = pygame.time.get_ticks() / 1000.0
    for k in range(3):
        ph = (t * 1.5 + k / 3.0) % 1.0
        rr = int(3 + ph * 7)
        dc.circle(screen, (110, 110, 116), (int(x + 87 - ph * 22), int(g - 76 - ph * 26)), rr)
    # 推铲支臂
    dc.line(screen, YD, (x + 96, g - 22), (x + 132, g - 20), 6)
    dc.line(screen, YD, (x + 100, g - 44), (x + 132, g - 38), 5)
    # 推铲：弧形铲板 + 铲刃
    blade = [(x + 132, g - 58), (x + 142, g - 60), (x + 148, g - 40), (x + 147, g - 18), (x + 150, g), (x + 134, g), (x + 136, g - 22), (x + 136, g - 42)]
    dc.polygon(screen, Y, blade)
    dc.polygon(screen, YD, blade, 2)
    dc.line(screen, (120, 120, 126), (x + 134, g - 1), (x + 150, g - 1), 3)

# ================= 托盘 =================
cmds = []
running = [True]

def start_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((14, 28, 50, 62), fill="white", outline="gray")
    d.ellipse((20, 6, 44, 30), fill="white", outline="gray")

    def quit_app(icon, item):
        running[0] = False
        icon.stop()

    def level_item(name):
        return pystray.MenuItem(name, lambda i, it: cmds.append(name),
                                checked=lambda it: snow["level"] == name, radio=True)

    menu = pystray.Menu(
        pystray.MenuItem("雪量", pystray.Menu(*[level_item(k) for k in SNOW_LEVELS])),
        pystray.MenuItem("换衣服", lambda i, it: cmds.append("hat")),
        pystray.MenuItem("重新堆雪人", lambda i, it: cmds.append("build")),
        pystray.MenuItem("叫推土机", lambda i, it: cmds.append("plow")),
        pystray.MenuItem("清空积雪", lambda i, it: cmds.append("clear")),
        pystray.MenuItem("退出", quit_app),
    )
    pystray.Icon("snow", img, "桌面下雪", menu).run()

# ================= 测试替身（只在非 Windows 测试时用）=================
_frame = [0]
def _test_windows():
    f = _frame[0]
    w = [(1, 200, 250, 700, 600, True), (2, 600, 380, 1000, 650, True)]
    if f > TEST_FRAMES * 0.85:
        w = w[1:]
    return w

def _test_cursor():
    f = _frame[0]
    if f == 100:                          # 点一下雪人
        return int(balls[1].x - 10), int(balls[1].y)
    if f == int(TEST_FRAMES * 0.7):       # 去点一个雪球
        b = balls[0]
        return int(b.x), int(b.y)
    return 50, 50

def _test_click():
    return _frame[0] in (100, int(TEST_FRAMES * 0.7))

# ================= 主循环 =================
def main():
    global _prev_mask
    if not TEST_FRAMES:
        threading.Thread(target=start_tray, daemon=True).start()
    clock = pygame.time.Clock()
    frame = 0
    t0 = time.time()
    if TEST_FRAMES:
        ground.h[:] = np.random.uniform(20, 34, ground.n)
    while running[0]:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running[0] = False
        while cmds:
            c = cmds.pop(0)
            if c == "hat":
                set_outfit(outfit["i"] + 1)
            elif c == "build":
                rebuild(sm["bx"] if sm["state"] == "stand" else balls[0].x)
            elif c == "plow":
                call_plow()
            elif c in SNOW_LEVELS:
                set_snow(c)
            elif c == "clear":
                ground.h[:] = 0
                for s in z_order:
                    s.h[:] = 0
        t = time.time() - t0
        if frame % WIN_SCAN_EVERY == 0:
            scan_windows()
        read_mouse()
        step_flakes(t)
        step_particles()
        mouse_push()
        plow_step()
        relax(ground, 2)
        for s in z_order:
            relax(s, 2)
        step_snowman()
        if not plow["on"] and ground.h.mean() > PLOW_TRIGGER:
            call_plow()
        if frame == 60 * 4:
            call_plow()

        # 只清空上一帧画过的格子
        prev_runs = _mask_runs(_prev_mask)
        for r in prev_runs:
            screen.fill(KEY, r)
        DIRTY.clear()
        for s in reversed(z_order):
            draw_surface(s)
        draw_surface(ground)
        draw_snowman()
        draw_plow()
        n = snow["n"]
        blits = [(sprites[int(r)], (x - r, y - r)) for x, y, r in zip(fx[:n].tolist(), fy[:n].tolist(), fr[:n].tolist())]
        ai = np.nonzero(P["alive"])[0]
        if len(ai):
            pv, px, py = P["v"][ai], P["x"][ai], P["y"][ai]
            pr = np.where(pv <= 0, 0, np.clip(np.sqrt(np.maximum(pv, 0)) * 1.8, 1, 5).astype(int))
            for x, y, r in zip(px.tolist(), py.tolist(), pr.tolist()):
                if r == 0:
                    blits.append((mist_sprite, (x - 1, y - 1)))
                else:
                    blits.append((sprites[r], (x - r, y - r)))
        DIRTY.extend(screen.blits(blits))
        cur_mask = _rects_to_mask(DIRTY)
        pygame.display.update(_mask_runs(cur_mask | _prev_mask))   # 只把变化的地方推给 Windows
        _prev_mask = cur_mask
        if TEST_FRAMES and frame % 100 == 0:
            print("刷新面积占比", round(float((cur_mask | _prev_mask).mean()), 3))
        frame += 1
        _frame[0] = frame
        if TEST_FRAMES:
            if frame in TEST_SHOTS:
                pygame.image.save(screen, f"test_{frame}.png")
            if frame >= TEST_FRAMES:
                break
        else:
            clock.tick(FPS)
    pygame.quit()

TEST_SHOTS = set(int(x) for x in os.environ.get("SNOW_TEST_SHOTS", "").split(",") if x)

if __name__ == "__main__":
    main()
