"""Checks the game-window capture (mfm.game_window_grab) on this machine: opens a
window titled "MapleStory" with known colours, covers part of it with another window,
opens a bigger browser-like window with "MapleStory" in its title, and reads the game's. Prints the result; exit code 0 = read right, 1 = wrong, 2 = not read."""
import os
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import mfm  # noqa: E402

root = tk.Tk()
root.title("MapleStory")
root.geometry("640x480+60+60")
cv = tk.Canvas(root, width=640, height=480, bg="#3060c0", highlightthickness=0)
cv.pack()
cv.create_rectangle(40, 40, 300, 240, fill="#ff8000", outline="")
cover = tk.Toplevel(root)
cover.title("cover")
cover.geometry("300x300+120+120")
tk.Frame(cover, bg="#00ff00", width=300, height=300).pack()
# a bigger window with "MapleStory" in its title too (a browser on a MapleStory page):
# never the one read
decoy = tk.Toplevel(root)
decoy.title("MapleStory Classic - YouTube — Mozilla Firefox")
decoy.geometry("900x700+760+60")
tk.Frame(decoy, bg="#c00000", width=900, height=700).pack()
result = {}


def check():
    deadline = time.time() + 20
    img = None
    while time.time() < deadline and img is None:
        img = mfm.game_window_grab({})
        time.sleep(0.5)
    if img is None:
        result["code"], result["text"] = 2, "the game window wasn't read (None)"
        return
    w, h = img.size
    corner = img.getpixel((w - 10, h - 10))   # (bottom right: macOS includes the title bar)
    top = h - 480 if h > 480 else 0           # the title bar's height, if it's included
    under = img.getpixel((200, 200 + top))    # orange box, under the covering window
    t0 = time.perf_counter()
    for _ in range(10):
        mfm.game_window_grab({})
    ms = (time.perf_counter() - t0) * 100
    ok = abs(w - 640) <= 80 and 0 <= h - 480 <= 80 and corner[2] > 150 and corner[0] < 100 \
        and under[0] > 200 and 90 < under[1] < 170 and under[2] < 60
    result["code"] = 0 if ok else 1
    result["text"] = (f"{'OK' if ok else 'WRONG'}: {w}x{h}, corner {corner} (want blue), under the cover "
                      f"{under} (want orange), {ms:.1f} ms per read")


def run():
    time.sleep(2)
    try:
        check()
    except Exception as e:
        result["code"], result["text"] = 1, f"error: {type(e).__name__}: {e}"
    root.after(0, root.destroy)


threading.Thread(target=run, daemon=True).start()
root.after(60000, root.destroy)       # never hang the check
root.mainloop()
print(f"{sys.platform}: {result.get('text')}")
sys.exit(result.get("code", 1))
