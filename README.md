# hikview

Live view and playback for Hikvision DVRs on Linux, without the Windows-only
web plugin.

- **Live grid window**: every connected camera in one window; click a camera
  to open it full size.
- **Recording search**: list what the DVR recorded on a day, per channel.
- **Playback**: play a channel from any time.
- **Discovery**: finds the DVR on the LAN, so no address is needed.

Recording search goes through [HikLoad](https://github.com/Tedyst/HikLoad)'s
ISAPI client; video is played with `ffplay` over RTSP.

Tested with a DS-7108HGHI-K1 (Turbo HD DVR, firmware V4.30). Other Hikvision
DVRs and NVRs with ISAPI and RTSP should work.

## Requirements

- Linux with Python 3.10 or later
- `ffmpeg`, `ffplay` and `ffprobe` on the PATH (Fedora: `dnf install ffmpeg`,
  Debian/Ubuntu: `apt install ffmpeg`)
- A DVR user with remote live view and playback rights

## Install

```
git clone https://github.com/e4c5/hikview.git
cd hikview
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps hikload==1.1.4
ln -sf "$PWD/hikview" ~/.local/bin/hikview
```

hikload 1.1.4 pins 2022-era dependencies that no longer build on current
Python ([HikLoad#39](https://github.com/Tedyst/HikLoad/issues/39)), so it is
installed without them; `requirements.txt` brings current versions. pip's
conflict warnings are expected.

Desktop launcher (optional):

```
sed "s|@HIKVIEW@|$PWD/hikview|" hikview-live.desktop > ~/.local/share/applications/hikview-live.desktop
```

## Configure

Credentials go in `.env` beside `hikview.py` (git-ignored; `chmod 600` it):

```
USERNAME=admin
PASSWORD=...
HOST=192.168.1.64    # optional: found automatically when left out
```

Optional settings in `~/.config/hikview/config.ini`:

```
[dvr]
host = 192.168.1.64
channels = 8
aspect = 16/9
```

`aspect` is the shape the picture is shown at. DVRs recording "1080p Lite"
store a half-width 960×1080 frame, which looks squeezed unless it is set.

## Use

```
hikview gui                       # window with all cameras; click one to open it
hikview live                      # all cameras in one ffplay window
hikview live 3                    # camera 3, main stream
hikview list                      # today's recordings, every channel
hikview list -d 2026-11-17 -c 2   # one day, one channel
hikview play 2 17:30              # channel 2 from 17:30 today, for an hour
hikview play 2 "2026-11-16 08:00" -m 15
hikview discover                  # Hikvision devices on the LAN
```

In the `gui` window: click a camera to open it, `F` full screen, `R` rescan
cameras, `Q` quit. In ffplay windows: `q` quits, `f` full screen, space pauses.

`hikview live --rescan` scans the cameras again after one is added or
removed; `hikview live --check 5` decodes the grid for 5 s without a window.
`HIKVIEW_DEBUG=1` prints the commands it runs, with the password masked.

## Notes

- Times are the DVR's local time. If the DVR clock was wrong, older footage
  is filed under the date it believed it was.
- Some DVRs refuse RTSP setups made in parallel, so the first live view scans
  the channels one at a time and caches the result in
  `~/.cache/hikview/streams.json`. A channel whose sub stream is off is shown
  from its main stream. Inputs the DVR reports as "NO VIDEO" are left out.
- Discovery uses Hikvision's SADP probe (UDP multicast on port 37020); the
  address it finds is remembered in `~/.cache/hikview/host`.

## License

MIT
