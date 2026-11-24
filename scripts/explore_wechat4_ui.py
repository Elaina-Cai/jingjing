import time
import win32gui

try:
    import uiautomation as uia
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "uiautomation"])
    import uiautomation as uia

hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
if not hwnd:
    print("微信窗口未找到！")
    exit(1)

print(f"微信窗口 HWND={hwnd}")

wechat = uia.WindowControl(ClassName="Qt51514QWindowIcon", searchDepth=1)
wechat.Exists(3)

print("\n=== 方案1: 深度遍历 MMUIRenderSubWindowHW ===")
render_win = wechat.PaneControl(ClassName="MMUIRenderSubWindowHW", searchDepth=3)
if render_win.Exists(3):
    print(f"找到渲染窗口: {render_win.Name} Class={render_win.ClassName}")
    walk_count = [0]
    def walk(ctrl, depth=0, max_depth=6, max_count=100):
        if depth > max_depth or walk_count[0] >= max_count:
            return
        walk_count[0] += 1
        indent = "  " * depth
        print(f"{indent}{ctrl.ControlTypeName} Name='{ctrl.Name}' Class='{ctrl.ClassName}' AutoId='{ctrl.AutomationId}'")
        try:
            for child in ctrl.GetChildren():
                walk(child, depth + 1, max_depth, max_count)
        except Exception:
            pass
    walk(render_win)
else:
    print("未找到渲染窗口")

print("\n=== 方案2: 用win32gui枚举子窗口 ===")
child_windows = []
def enum_children(hwnd_parent, depth=0, max_depth=5):
    if depth > max_depth:
        return
    def callback(child_hwnd, _):
        cls = win32gui.GetClassName(child_hwnd)
        title = win32gui.GetWindowText(child_hwnd)
        indent = "  " * depth
        child_windows.append((depth, child_hwnd, cls, title))
        print(f"{indent}HWND={child_hwnd} Class='{cls}' Title='{title}'")
        enum_children(child_hwnd, depth + 1, max_depth)
    win32gui.EnumChildWindows(hwnd_parent, callback, None)

enum_children(hwnd)
print(f"\n共找到 {len(child_windows)} 个子窗口")

print("\n=== 方案3: 检查是否有IAccessible ===")
try:
    from comtypes.client import GetModule
    GetModule("oleacc.dll")
    accessible = wechat.GetChildren()
    print(f"子控件数: {len(accessible)}")
except Exception as e:
    print(f"IAccessible失败: {e}")
