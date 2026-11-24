import win32gui
import subprocess

results = []

def callback(hwnd, _):
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    if "wechat" in cls.lower() or "wechat" in title.lower() or "微信" in title:
        results.append((hwnd, cls, title))

win32gui.EnumWindows(callback, None)

print(f"Found {len(results)} WeChat-related windows:")
for hwnd, cls, title in results:
    print(f"  HWND={hwnd} Class={cls} Title={title}")

print("\n--- Checking WeChat version ---")
try:
    import win32process
    for hwnd, cls, title in results:
        if "微信" in title:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            print(f"  PID: {pid}")
            try:
                import psutil
                p = psutil.Process(pid)
                print(f"  Exe: {p.exe()}")
                print(f"  Cwd: {p.cwd()}")
            except Exception:
                pass
except Exception as e:
    print(f"  Error: {e}")

print("\n--- Trying UIAutomation ---")
try:
    import uiautomation as uia
    wechat_win = uia.WindowControl(ClassName="Qt51514QWindowIcon", searchDepth=1)
    if wechat_win.Exists(3):
        print(f"  UIA found: Name={wechat_win.Name} Class={wechat_win.ClassName}")
    else:
        print("  UIA: Qt51514QWindowIcon not found")

    wechat_win2 = uia.WindowControl(Name="微信", searchDepth=1)
    if wechat_win2.Exists(3):
        print(f"  UIA found by name: Name={wechat_win2.Name} Class={wechat_win2.ClassName}")
    else:
        print("  UIA: '微信' window not found")
except Exception as e:
    print(f"  UIA error: {e}")
