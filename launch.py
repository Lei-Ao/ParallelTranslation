"""Open the local UI once the server and its static assets are ready."""
import threading
import urllib.error
import urllib.request
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8765"


def server_ready():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL + "/api/session", timeout=1) as response:
            if response.status != 200 or b'"token"' not in response.read(4096):
                return False
        for asset, mime in [("style.css", "text/css"), ("translation-queue.js", "javascript"), ("app.js", "javascript")]:
            with opener.open(URL + "/static/" + asset, timeout=1) as response:
                if response.status != 200 or mime not in response.headers.get("Content-Type", ""):
                    return False
        return True
    except (OSError, urllib.error.URLError):
        return False


def open_when_ready(stop):
    for _ in range(60):
        if stop.is_set():
            return
        if server_ready():
            webbrowser.open(URL)
            return
        if stop.wait(.5):
            return
    print("Browser could not be opened automatically. Visit " + URL, flush=True)


def main():
    if server_ready():
        print("Parallel is already running. Opening " + URL, flush=True)
        webbrowser.open(URL)
        return
    stop = threading.Event()
    thread = threading.Thread(target=open_when_ready, args=(stop,), daemon=True)
    thread.start()
    try:
        uvicorn.run("app:app", host="127.0.0.1", port=8765)
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
