#!/usr/bin/env python3
"""Live view and playback for a Hikvision DVR on Linux.

Recording search goes through hikload's ISAPI client; video is shown with
ffplay over RTSP. Credentials come from ~/.config/hikview/config.ini.

  hikview live              all channels in a grid (sub streams)
  hikview live 3            channel 3 full size (main stream)
  hikview gui               live grid window; click a camera to open it
  hikview discover          find Hikvision devices on the LAN
  hikview list [-d DATE] [-c CH]   recordings on a day
  hikview play CH TIME      play back from TIME (HH:MM or YYYY-MM-DD HH:MM)
"""

import argparse
import configparser
import datetime as dt
import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

from hikload.hikvisionapi import HikvisionServer, HikvisionException, xml2dict

SEARCH_XML = """<CMSearchDescription version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<searchID>{id}</searchID>
<trackIDList><trackID>{track}</trackID></trackIDList>
<timeSpanList><timeSpan><startTime>{start}</startTime><endTime>{end}</endTime></timeSpan></timeSpanList>
<maxResults>64</maxResults>
</CMSearchDescription>"""

CONFIG = os.path.expanduser("~/.config/hikview/config.ini")
CACHE = os.path.expanduser("~/.cache/hikview/streams.json")
HOST_CACHE = os.path.expanduser("~/.cache/hikview/host")
CELL_W, CELL_H = 640, 360
LOW_DELAY = ["-fflags", "nobuffer", "-flags", "low_delay"]
# Open each stream quickly instead of analysing 5 s of it.
FAST_OPEN = ["-analyzeduration", "1000000", "-probesize", "200000"]


def read_env(path):
    """KEY=value lines from a .env file (quotes stripped)."""
    out = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    return out


def discover(wait=3):
    """Find Hikvision devices on the LAN with an SADP probe (UDP multicast, port 37020).

    Returns [(ip, model, firmware)].
    """
    group, port = "239.255.255.250", 37020
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    probe = ('<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{}</Uuid>'
             '<Types>inquiry</Types></Probe>').format(str(uuid.uuid4()).upper())
    sock.sendto(probe.encode(), (group, port))
    sock.settimeout(0.5)
    found, end = {}, time.time() + wait
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(8192)
        except socket.timeout:
            continue
        text = data.decode(errors="replace")
        if "<ProbeMatch>" not in text or addr[0] in found:
            continue

        def tag(name):
            m = re.search(r"<{0}>([^<]*)</{0}>".format(name), text)
            return m.group(1) if m else "?"
        found[addr[0]] = (tag("IPv4Address"), tag("DeviceDescription"), tag("SoftwareVersion"))
    sock.close()
    return list(found.values())


def find_host():
    """The DVR's address: remembered from the last discovery, or discovered now."""
    try:
        with open(HOST_CACHE) as f:
            return f.read().strip()
    except OSError:
        pass
    print("No host configured; looking for a Hikvision device on the LAN...", file=sys.stderr)
    devices = discover()
    if not devices:
        sys.exit("No device found. Set HOST in .env or host in {}".format(CONFIG))
    ip, model, _ = devices[0]
    print("Using {} at {} (set HOST in .env to choose another)".format(model, ip), file=sys.stderr)
    os.makedirs(os.path.dirname(HOST_CACHE), exist_ok=True)
    with open(HOST_CACHE, "w") as f:
        f.write(ip)
    return ip


def load_config():
    """config.ini for settings; USERNAME/PASSWORD in the .env beside this file win."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(CONFIG)
    c = cp["dvr"] if cp.has_section("dvr") else {}
    env = read_env(os.path.join(os.path.dirname(os.path.realpath(__file__)), ".env"))
    password = env.get("PASSWORD") or c.get("password")
    if not password or password == "CHANGE_ME":
        sys.exit("No password: set PASSWORD in .env or password in {}".format(CONFIG))
    return {
        "host": env.get("HOST") or c.get("host") or find_host(),
        "user": env.get("USERNAME") or c.get("user", "admin"),
        "password": password,
        "channels": int(c.get("channels", 8)),
        "aspect": c.get("aspect", "16/9"),
    }


def rtsp_base(cfg):
    return "rtsp://{}:{}@{}:554".format(
        quote(cfg["user"], safe=""), quote(cfg["password"], safe=""), cfg["host"])


def live_url(cfg, channel, stream):
    """stream 1 = main, 2 = sub."""
    return "{}/Streaming/Channels/{}0{}".format(rtsp_base(cfg), channel, stream)


def isapi_time(t):
    # The DVR reads these as its own local time despite the Z.
    return t.strftime("%Y%m%dT%H%M%SZ")


def playback_url(cfg, channel, start, end):
    return "{}/Streaming/tracks/{}01?starttime={}&endtime={}".format(
        rtsp_base(cfg), channel, isapi_time(start), isapi_time(end))


def mask(text):
    """Hide the password in RTSP URLs that ffmpeg echoes in its messages."""
    return re.sub(r"(rtsp://[^:/@\s]+:)[^@\s]+@", r"\1***@", text)


def debug(cmd):
    if os.environ.get("HIKVIEW_DEBUG"):
        print(mask(" ".join(shlex.quote(c) for c in cmd)), file=sys.stderr)


def run(cmd, **kw):
    debug(cmd)
    return subprocess.run(cmd, **kw)


def popen(cmd, **kw):
    """Start a command whose stderr is shown with the password masked."""
    debug(cmd)
    # Binary pipes: stdout may carry raw video.
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, **kw)

    def relay():
        for line in p.stderr:
            sys.stderr.write(mask(line.decode(errors="replace")))
    threading.Thread(target=relay, daemon=True).start()
    return p


def ffplay(url, title, aspect):
    p = popen(["ffplay", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               *LOW_DELAY, *FAST_OPEN, "-window_title", title, "-vf", "setdar=" + aspect, url])
    p.wait()


def has_signal(url):
    """True if the stream opens within a few seconds."""
    r = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000", *FAST_OPEN,
             "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=10)
    return r.returncode == 0 and r.stdout.strip() != ""


def read_cache():
    """{channel: stream} from the last scan, or None."""
    try:
        with open(CACHE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return None


def write_cache(streams):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(streams, f)


def connected_inputs(cfg):
    """Channels with a camera signal (the DVR's own "NO VIDEO" check), or None if unknown."""
    try:
        r = requests.get("http://{}/ISAPI/System/Video/inputs/channels".format(cfg["host"]),
                         auth=HTTPDigestAuth(cfg["user"], cfg["password"]), timeout=5)
        r.raise_for_status()
    except requests.RequestException:
        return None
    found = set()
    for block in re.findall(r"<VideoInputChannel[^>]*>(.*?)</VideoInputChannel>", r.text, re.S):
        ch = re.search(r"<id>(\d+)</id>", block)
        res = re.search(r"<resDesc>([^<]*)</resDesc>", block)
        if ch and not (res and res.group(1).strip().upper() == "NO VIDEO"):
            found.add(int(ch.group(1)))
    return found or None


def grid_streams(cfg, rescan=False, log=None):
    """{channel: url} for the grid, using the cached channel scan when there is one.

    The DVR refuses parallel RTSP setups, so the scan probes one channel at a time
    (a few seconds each). It uses the sub stream, or the main stream where the sub
    stream is off.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    streams = None if rescan else read_cache()
    if streams is None:
        log("Scanning {} channels (cached for next time)...".format(cfg["channels"]))
        streams = {}
        for ch in range(1, cfg["channels"] + 1):
            log("Scanning channel {}...".format(ch))
            for stream in (2, 1):
                if _safe_signal(live_url(cfg, ch, stream)):
                    streams[ch] = stream
                    break
        write_cache(streams)
    # Leave out inputs with no camera plugged in.
    live = connected_inputs(cfg)
    return {ch: live_url(cfg, ch, s) for ch, s in sorted(streams.items())
            if live is None or ch in live}


def grid_shape(n):
    cols = math.ceil(math.sqrt(n))
    return cols, math.ceil(n / cols)


def grid_command(urls, cell_w=CELL_W, cell_h=CELL_H, labels=True):
    """ffmpeg command (without output) that tiles the streams into one picture."""
    chans = list(urls)
    cols, _ = grid_shape(len(chans))
    # fatal: joining HEVC streams mid-GOP prints a burst of harmless decode errors.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "fatal", "-nostdin"]
    for ch in chans:
        cmd += ["-rtsp_transport", "tcp", *LOW_DELAY, *FAST_OPEN, "-i", urls[ch]]
    parts, pads = [], []
    for i, ch in enumerate(chans):
        text = (",drawtext=text='{}':x=8:y=8:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.5"
                .format(ch) if labels else "")
        parts.append("[{i}:v]scale={w}:{h},setsar=1,fps=15{t}[v{i}]".format(
            i=i, w=cell_w, h=cell_h, t=text))
        pads.append("[v{}]".format(i))
    if len(chans) == 1:
        graph = parts[0].replace("[v0]", "")
    else:
        layout = "|".join("{}_{}".format((i % cols) * cell_w, (i // cols) * cell_h)
                          for i in range(len(chans)))
        graph = ";".join(parts) + ";{}xstack=inputs={}:layout={}:fill=black".format(
            "".join(pads), len(chans), layout)
    return cmd + ["-filter_complex", graph, "-an"]


def live_grid(cfg, check=0, rescan=False):
    urls = grid_streams(cfg, rescan)
    if not urls:
        sys.exit("No channel answered. Check the password and that RTSP is enabled.")
    print("Showing channels {}".format(", ".join(map(str, urls))), file=sys.stderr)
    cols, rows = grid_shape(len(urls))
    cmd = grid_command(urls)
    if check:
        # Decode for a few seconds without a window.
        r = subprocess.run(cmd + ["-t", str(check), "-f", "null", "-"],
                           stderr=subprocess.PIPE, text=True)
        err = mask(r.stderr.strip())
        print("grid {}x{}: {}".format(cols, rows, "OK" if r.returncode == 0 else "FAILED"))
        if err:
            print(err, file=sys.stderr)
        return
    cmd += ["-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "nut", "-"]
    play = ["ffplay", "-hide_banner", "-loglevel", "error", *LOW_DELAY,
            "-window_title", "DVR live ({}x{})".format(cols, rows), "-"]
    src = popen(cmd, stdout=subprocess.PIPE)
    try:
        subprocess.run(play, stdin=src.stdout)
    finally:
        src.terminate()


def _safe_signal(url):
    try:
        return has_signal(url)
    except subprocess.TimeoutExpired:
        return False


def server(cfg):
    return HikvisionServer(cfg["host"], cfg["user"], cfg["password"])


def search(cfg, channel, start, end):
    """Recording segments for a channel between two local datetimes."""
    # hikload's dict2xml needs a dict made by its own xml2dict (it keeps the attributes).
    data = xml2dict(SEARCH_XML.format(
        id=uuid.uuid4(), track="{}01".format(channel),
        start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=end.strftime("%Y-%m-%dT%H:%M:%SZ")).encode())
    res = server(cfg).ContentMgmt.search.get(data)["CMSearchResult"]
    if res.get("responseStatusStrg") == "NO MATCHES" or not res.get("matchList"):
        return []
    items = res["matchList"]["searchMatchItem"]
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items:
        span = it["timeSpan"]
        out.append((parse_dvr_time(span["startTime"]), parse_dvr_time(span["endTime"])))
    return merge(out)


def parse_dvr_time(s):
    return dt.datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")


def merge(spans, gap=dt.timedelta(seconds=5)):
    """Join back-to-back file segments into continuous spans."""
    spans = sorted(spans)
    out = []
    for s, e in spans:
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def cmd_list(cfg, args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time(23, 59, 59))
    chans = [args.channel] if args.channel else range(1, cfg["channels"] + 1)
    for ch in chans:
        try:
            spans = search(cfg, ch, start, end)
        except HikvisionException as e:
            sys.exit("Search failed: {}".format(e))
        if not spans:
            print("ch{}  no recordings".format(ch))
            continue
        for s, e in spans:
            print("ch{}  {} - {}  ({})".format(
                ch, s.strftime("%H:%M:%S"), e.strftime("%H:%M:%S"), e - s))


def parse_when(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.combine(dt.date.today(), dt.datetime.strptime(s, fmt).time())
        except ValueError:
            pass
    sys.exit("Can't read time {!r}: use HH:MM or YYYY-MM-DD HH:MM".format(s))


def cmd_play(cfg, args):
    start = parse_when(args.time)
    end = start + dt.timedelta(minutes=args.minutes)
    ffplay(playback_url(cfg, args.channel, start, end),
           "ch{} from {}".format(args.channel, start.strftime("%Y-%m-%d %H:%M")), cfg["aspect"])


def cmd_live(cfg, args):
    if args.channel:
        ffplay(live_url(cfg, args.channel, 1), "ch{} live".format(args.channel), cfg["aspect"])
    else:
        live_grid(cfg, args.check, args.rescan)


def main():
    p = argparse.ArgumentParser(prog="hikview", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("live", help="live view: grid, or one channel")
    lv.add_argument("channel", nargs="?", type=int)
    lv.add_argument("--check", type=int, default=0, metavar="SECONDS",
                    help="decode the grid for SECONDS without a window, to test it")
    lv.add_argument("--rescan", action="store_true",
                    help="scan the channels again (after adding or removing a camera)")
    ls = sub.add_parser("list", help="list recordings on a day")
    ls.add_argument("-d", "--date", help="YYYY-MM-DD (default today)")
    ls.add_argument("-c", "--channel", type=int)
    sub.add_parser("gui", help="live view window: click a camera to open it")
    sub.add_parser("discover", help="find Hikvision devices on the LAN")
    pl = sub.add_parser("play", help="play back a recording")
    pl.add_argument("channel", type=int)
    pl.add_argument("time", help="HH:MM, or 'YYYY-MM-DD HH:MM'")
    pl.add_argument("-m", "--minutes", type=int, default=60, help="how long to play (default 60)")
    args = p.parse_args()
    if args.cmd == "discover":
        devices = discover()
        for ip, model, firmware in devices:
            print("{}  {}  {}".format(ip, model, firmware))
        if not devices:
            print("No Hikvision device answered.")
        return
    if args.cmd == "gui":
        import gui
        gui.main()
        return
    cfg = load_config()
    {"live": cmd_live, "list": cmd_list, "play": cmd_play}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
