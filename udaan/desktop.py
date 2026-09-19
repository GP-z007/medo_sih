import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from udaan.config import settings


def clear_stale_display_runtime(display, lock_root=Path("/tmp"), socket_root=Path("/tmp/.X11-unix")):
    """Remove only dead local X runtime files for the requested numeric display."""
    match = re.fullmatch(r":(\d+)", display)
    if not match:
        return False
    number = match.group(1)
    lock = lock_root / f".X{number}-lock"
    socket = socket_root / f"X{number}"
    if not lock.exists() and not socket.exists():
        return False
    try:
        owner = lock.stat().st_uid if lock.exists() else socket.stat().st_uid
        pid_text = lock.read_text().strip() if lock.exists() else ""
    except OSError:
        return False
    if owner != os.getuid() or (pid_text.isdigit() and Path(f"/proc/{pid_text}").exists()):
        return False
    changed = False
    for path in (lock, socket):
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            pass
    return changed


def prepare_web_root():
    """Own the presentation without modifying the system noVNC installation."""
    source, target = Path("/usr/share/novnc"), Path("var/desktop-web")
    shutil.copytree(source, target, dirs_exist_ok=True)
    html = target / "vnc.html"
    text = html.read_text().replace("<title>noVNC</title>", "<title>Udaan Browser</title>")
    text = text.replace("<span>no</span><br>VNC", "Udaan Browser").replace("<span>no</span>VNC", "Udaan Browser")
    text = text.replace("noVNC encountered an error:", "Browser View encountered an error:")
    text = text.replace("</head>", "<style>.noVNC_logo {font: 500 16px sans-serif !important; text-shadow:none !important; letter-spacing:normal !important;}</style></head>")
    from udaan.branding import ASSETS
    shutil.copy2(ASSETS/'udaan_logo.svg', target/'udaan-icon.svg')
    shutil.copy2(ASSETS/'udaan_eagle_wordmark.svg', target/'udaan-wordmark.svg')
    text = re.sub(r'<link[^>]+rel=["\'](?:icon|shortcut icon|apple-touch-icon)["\'][^>]*>', '', text)
    text = text.replace('</head>', '<link rel="icon" type="image/svg+xml" href="udaan-icon.svg"></head>')
    html.write_text(text)
    ui = target / "app" / "ui.js"
    ui.write_text(ui.read_text().replace('const PAGE_TITLE = "noVNC";', 'const PAGE_TITLE = "Udaan Browser";').replace('document.title = e.detail.name + " - " + PAGE_TITLE;', 'document.title = PAGE_TITLE;'))
    return target.resolve()


def run():
    required = ["Xvfb", "x11vnc", "websockify", "openbox", "xdpyinfo"]
    missing = [x for x in required if not shutil.which(x)]
    if missing:
        raise RuntimeError("Install desktop packages: " + ", ".join(missing))
    web_root = prepare_web_root()
    display = settings().display or ":99"
    root = Path(".desktop-secrets")
    root.mkdir(mode=0o700, exist_ok=True)
    password_file, auth_file = root / "password", root / "vnc-auth"
    if not password_file.exists():
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(6)[:8])
    password = password_file.read_text().strip()
    if not auth_file.exists():
        result = subprocess.run(["x11vnc", "-storepasswd", str(auth_file)], input=f"{password}\n{password}\ny\n",
                                text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0 or not auth_file.exists():
            raise RuntimeError("Could not initialize local VNC authentication file")
        auth_file.chmod(0o600)
    env = {**os.environ, "DISPLAY": display}
    processes = []
    log_dir = Path("var/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "desktop.log").open("a") as log:
        try:
            def launch(args):
                process = subprocess.Popen(args, env=env, stdout=log, stderr=log)
                processes.append(process)
                return process
            check = subprocess.run(["xdpyinfo", "-display", display], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode:
                clear_stale_display_runtime(display)
                launch(["Xvfb", display, "-screen", "0", "1366x900x24", "-nolisten", "tcp"])
                for _ in range(30):
                    if subprocess.run(["xdpyinfo", "-display", display], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Visible desktop display failed to start")
            launch(["openbox"])
            launch(["x11vnc", "-display", display, "-rfbport", "5901", "-desktop", "Udaan Browser", "-localhost", "-forever", "-shared", "-rfbauth", str(auth_file)])
            launch(["websockify", "--web=" + str(web_root), "127.0.0.1:6080", "127.0.0.1:5901"])
            print(f"Udaan visible desktop: http://127.0.0.1:6080/vnc.html\nSet DISPLAY={display} for API/worker.\nLocal VNC password: {password_file.resolve()} (file contents are private)", flush=True)
            while all(p.poll() is None for p in processes):
                time.sleep(1)
            raise RuntimeError("A desktop component exited; inspect var/logs/desktop.log")
        except KeyboardInterrupt:
            pass
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
