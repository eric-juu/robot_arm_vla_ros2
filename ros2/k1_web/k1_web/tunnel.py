"""
Expose the web interface through a Cloudflare quick tunnel and print its public URL.

    ros2 run k1_web tunnel [http://127.0.0.1:8080]

Runs `cloudflared tunnel --url <local url>`, which needs no Cloudflare account. The public
https://<random>.trycloudflare.com URL changes every time this starts. Anyone with the URL
reaches the login page, so the K1_WEB_TOKEN is what protects the arm.
"""
import re
import shutil
import signal
import subprocess
import sys

DEFAULT_LOCAL_URL = "http://127.0.0.1:8080"
# quick tunnel hostnames are random hyphenated words; this skips api.trycloudflare.com in the startup log
PUBLIC_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
INSTALL_HINT = """cloudflared is not installed. Install it with:
  curl -L -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cloudflared.deb
(use cloudflared-linux-arm64.deb on ARM, e.g. a Raspberry Pi or Jetson)"""


def find_public_url(line: str) -> str | None:
    match = PUBLIC_URL_RE.search(line)
    return match.group(0) if match else None


def main():
    # launch_ros may append --ros-args ...; the local URL is the only argument we use
    args = sys.argv[1:sys.argv.index("--ros-args")] if "--ros-args" in sys.argv else sys.argv[1:]
    local_url = args[0] if args else DEFAULT_LOCAL_URL

    if shutil.which("cloudflared") is None:
        print(INSTALL_HINT, file=sys.stderr)
        sys.exit(1)

    # turn SIGTERM into KeyboardInterrupt so cloudflared is always stopped with us
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", local_url],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(f"[tunnel] starting a Cloudflare quick tunnel to {local_url} ...", flush=True)
    announced = False
    try:
        for line in proc.stdout:
            url = find_public_url(line)
            if url and not announced:
                announced = True
                bar = "=" * (len(url) + 4)
                print(f"\n{bar}\n  {url}\n{bar}\n"
                      "[tunnel] open this from any browser and sign in with your token.\n"
                      "[tunnel] it can take ~30 s before the link works, and it changes on every restart.\n",
                      flush=True)
            elif " ERR " in line:
                print(f"[tunnel] {line.rstrip()}", flush=True)
        sys.exit(proc.wait())
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
