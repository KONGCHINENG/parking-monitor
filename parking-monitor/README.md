# Parking Spot Monitor (ArUco + WiFi camera)

An ArUco marker is placed on the ground of each parking spot. If the camera sees the marker, the spot is **free**. If a car covers it, the spot is **occupied**.
On every confirmed change the monitor:

* writes `parking_status.json` into **iCloud Drive/Parking**, and
* sends a push to your iPhone via **ntfy**.

```
 camera ──frames──> source thread ──newest frame every 1 s──> ArUco detector
                                                               │ ids seen
                    reference marker 9 seen? ── no ──> camera "blind": never mark occupied
                                                               │
                                     per-spot debounce (10 s) ─┴─> JSON (atomic) + ntfy push
```

## Contents

| File | What it does |
|---|---|
| `parking_monitor.py` | Command line: `run`, `gen`, `probe`, `test-notify` |
| `config.yaml` | Everything you set: camera, marker IDs → spot names, timings, outputs |
| `parking/sources.py` | Frame sources: `mjpeg`, `rtsp`, `a9v720`, `file`. Each reconnects automatically |
| `parking/detector.py` | `cv2.aruco.ArucoDetector` + CLAHE contrast boost |
| `parking/state.py` | Debounced state machines (pure logic, unit tested) |
| `parking/monitor.py` | Main loop, JSON/notification triggers, `--show` overlay |
| `parking/output.py` | Atomic JSON write and the ntfy client |
| `parking/markers.py` | Printable marker PNGs + true-size PDF |
| `parking/probe.py` | Step 1 port/RTSP/ONVIF probe |
| `tests/` | State machine, synthetic-frame, and stream-source tests |
| `deploy/` | Windows Task Scheduler, macOS launchd, Raspberry Pi systemd |

---

## Demo mode (no internet needed)

For a live demo, run everything on the camera's own WiFi:

1. Join the PC to the camera's WiFi (`Nax_...`).
2. Double-click **run_monitor.bat**.
   * A window opens on the PC with markers and spot states drawn.
   * The console prints the phone link, e.g. `http://192.168.169.2:8080`.
3. Join the **iPhone to the same `Nax_...` WiFi**. iOS will say "No Internet Connection"; tap *Keep* / *Use without internet*.
4. Open that link in Safari. The page updates every second: free count, each spot, live camera picture.

Notes:

* ntfy pushes and iCloud need internet, so they don't run during an offline demo. The phone page replaces them.
* Demo timings in `config.yaml` are `confirm_seconds: 3` and `camera_alert_seconds: 10`, so changes show quickly.
* If the phone can't open the page, the camera's WiFi may block device-to-device traffic. Show the PC window instead.

## Phone app + 24/7 operation

```
camera ──Nax WiFi──> USB WiFi adapter ─┐
                                       PC: parking monitor (starts at log-on)
internet <──FOREFRONT── built-in WiFi ─┘      │ app page on 127.0.0.1:8080 (PC only)
                                              │
iPhone (anywhere) ──Tailscale (private, encrypted)──> https://<pc-name>.<tailnet>.ts.net
iPhone (app closed) <── ntfy push ── PC
```

**Before the adapter arrives:** do steps 2–4 with **run_preview.bat** running. It serves the app from the sample video, with no camera needed and notifications off, so Tailscale and the home-screen app are ready in advance.
**Check everything at any time:** `powershell -ExecutionPolicy Bypass -File tools\check_network.ps1`. It only looks and changes nothing.

**1. USB WiFi adapter.** Plug it in. In Windows WiFi settings, pick the **new adapter** ("Wi-Fi 2") and connect it to `Nax_5c002000E0F3`; tick *Connect automatically*. Leave the built-in WiFi on FOREFRONT.
Check the internet still works on the PC. If it doesn't, Windows is sending internet traffic to the camera network. Make the camera adapter lower priority (admin PowerShell; check the adapter name with `Get-NetAdapter`):
```powershell
Set-NetIPInterface -InterfaceAlias "Wi-Fi 2" -InterfaceMetric 500
```

**2. Tailscale (free).**
1. Install Tailscale on the PC (tailscale.com/download) and on the iPhone (App Store), and sign in to **the same account** on both.
2. On the PC, run:
   ```powershell
   tailscale serve --bg 8080
   ```
   The first time, it asks you to enable HTTPS in the Tailscale admin page; follow the link. It prints your app address, e.g. `https://desktop-abc.tail1234.ts.net`. The page is only reachable by your own Tailscale devices, not the internet and not FOREFRONT.

**3. Install the app on the iPhone.**
1. With Tailscale switched on, open that https address in **Safari**.
2. Tap **Share → Add to Home Screen → Add**.

The "Parking" icon now opens the app full screen. Tailscale must be on (it can stay on permanently).

**4. Push notifications when the app is closed.** Install **ntfy** from the App Store, tap **+**, and subscribe to the topic in `config.yaml` (`notifications.topic`). Test it with `.venv\Scripts\python parking_monitor.py test-notify`.

**5. Start automatically and stay on.**
* Run `powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1`. The monitor then starts at log-on and restarts itself if it crashes.
* Stop the PC from sleeping when plugged in: *Settings → System → Power → Screen and sleep → "When plugged in, put my device to sleep after" = Never*.
* Optional: automatic sign-in after a reboot or power cut (`netplwiz`, untick "Users must enter a user name and password").

The startup task runs without `--lan`, so the app page is never open to FOREFRONT. Only Tailscale can reach it.

## 0. Install (Windows)

```powershell
cd parking-monitor
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest -q          # should say: 19 passed
```

On macOS or a Pi, use `python3 -m venv .venv` and `.venv/bin/...`.

> Install only `opencv-contrib-python`. If `opencv-python` is also installed, they overwrite each other and `cv2.aruco` breaks.

Try the whole thing without a camera:

```powershell
.venv\Scripts\python tools\make_test_video.py
# in config.yaml set  source: type: file
.venv\Scripts\python parking_monitor.py run --show
```

---

## Step 1 – Getting video out of the A9 / V720 camera

### 1A. Check for an undocumented local stream first

Find the camera's IP in your router's DHCP client list (the hostname is often `A9`, `anyka`, or a MAC starting with the Chinese module vendor). Then run:

```powershell
.venv\Scripts\python parking_monitor.py probe 192.168.1.23
```

The probe checks TCP 80/81/443/554/8000/8080/8554/8899/6123/23/1883. It also sends RTSP `OPTIONS` to the RTSP ports and an ONVIF WS-Discovery probe to UDP 3702. It only talks to that one IP.

* **RTSP open** → set `source.type: rtsp` and you're done.
* **Nothing open** (the normal result for this camera) → go to 1B or 1C.

### 1B. Fake-server mode: camera on your home WiFi (recommended for 24/7)

The camera normally phones home to `*.naxclow.com` (China). The [a9-v720](https://github.com/intx82/a9-v720) project includes a fake server that pretends to be that cloud. You point the camera's DNS lookups at your PC, and the fake server gives you an MJPEG URL.

What talks to what:

```
camera ──DNS: v720.naxclow.com?──> your DNS ──answers──> PC's IP
camera ──HTTP :80 (register)─────> fake server on PC
camera ──TCP+UDP :6123 (video)───> fake server on PC
camera ──MQTT :1883──────────────> mosquitto on PC
parking monitor ──HTTP──> http://127.0.0.1/dev/<uid>/live  (MJPEG)
```

**Steps marked ✋ are yours to do by hand. I have not changed any router, DNS, or firewall setting.**

1. ✋ **Give the PC a fixed IP.** Add a DHCP reservation in your router. Everything below points at this IP, written as `PC_IP`.

2. **Install the a9-v720 project** in its own folder with its own venv, so its `opencv-python` pin can't clash with ours:
   ```powershell
   git clone https://github.com/intx82/a9-v720 C:\a9-v720
   cd C:\a9-v720
   python -m venv .venv
   .venv\Scripts\pip install netifaces-plus numpy opencv-python tqdm xmltodict Pillow
   ```
   On Windows with Python 3.11, `netifaces==0.11.0` from their `requirements.txt` has no prebuilt wheel and needs a C compiler. `netifaces-plus` installs cleanly and provides the same `netifaces` module. I verified the wheel contents but have not run the fake server against a real camera.

3. **Install the MQTT broker.** Get [Mosquitto for Windows](https://mosquitto.org/download/) and tick "Service" during install. Mosquitto 2.x only listens on localhost by default, so edit `C:\Program Files\mosquitto\mosquitto.conf` and add:
   ```
   listener 1883 0.0.0.0
   allow_anonymous true
   ```
   Then restart the "Mosquitto Broker" service (services.msc).

4. ✋ **Windows Firewall.** Allow inbound traffic from the camera. Run in an **admin** PowerShell, and make sure your WiFi network profile is *Private*:
   ```powershell
   New-NetFirewallRule -DisplayName "A9 fake server" -Direction Inbound -Protocol TCP -LocalPort 80,6123,1883 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "A9 fake server UDP" -Direction Inbound -Protocol UDP -LocalPort 6123 -Action Allow -Profile Private
   ```

5. ✋ **DNS redirect.** Choose the option that fits your network:
   * **Router with custom DNS entries** (OpenWrt, ASUS-Merlin, MikroTik, pfSense, …): add `naxclow.com` and `naxclow.com.cn` (including all subdomains) → `PC_IP`. With dnsmasq the lines are:
     ```
     address=/naxclow.com/PC_IP
     address=/naxclow.com.cn/PC_IP
     ```
   * **Pi-hole / AdGuard Home:** add DNS rewrites `*.naxclow.com` and `*.naxclow.com.cn` → `PC_IP`. Then set it as the DNS server your router hands out via DHCP.
   * **Router can't do it** (ISP box, office network): see 1C (AP mode), or use a dedicated cheap travel router running OpenWrt just for the camera.

   Check from any device on that network: `nslookup v720.naxclow.com` should return `PC_IP`.
   Names the camera has been seen using: `v720.naxclow.com`, `v720.p2p.naxclow.com`, `p2p.v720.naxclow.com`, `dl2.naxclow.com`, `dl2.naxclow.com.cn`, `home.naxclow.com`, `logo.naxclow.com.cn`.

6. **Start the fake server.** Port 80 must be free; check with `netstat -ano | findstr ":80 "`, where IIS or Skype are common culprits.
   ```powershell
   cd C:\a9-v720
   .venv\Scripts\python src\a9_naxclow.py -s -v
   ```

7. ✋ **Move the camera to your home WiFi.**
   1. Factory-reset the camera.
   2. Join the PC to the camera's `Nax_xxxx` WiFi.
   3. In a second terminal, run:
      ```powershell
      .venv\Scripts\python src\a9_naxclow.py --set-wifi "YourSSID" "YourPassword"
      ```
      The camera supports 2.4 GHz only, and the password must be 8–36 characters.
   4. Rejoin the PC to your home WiFi.

   The camera reboots, joins your WiFi, resolves naxclow → your PC, and registers. You'll see it in the fake-server log.

8. **Confirm it works.**
   1. Open `http://127.0.0.1/dev/list` in a browser. You should see `[{"host": ..., "uid": "0800c00XXXXX"}]`.
   2. Open `http://127.0.0.1/dev/<uid>/live`. You should see live video.

9. Set `source.type: a9v720` and `a9v720.mode: fakeserver` in `config.yaml`. Leave `camera_id` empty to auto-pick the first camera.
   To start the fake server at log on, run `deploy\windows\install_fakeserver_task.ps1 -A9Dir C:\a9-v720`.

### 1C. AP mode (bench testing / PCs with Ethernet)

1. ✋ Join the PC's WiFi to the camera's `Nax_…` network. The camera is at `192.168.169.1`, control port TCP 6123.
2. Clone a9-v720 next to this project (`../a9-v720`).
3. Set `a9v720.mode: ap` and `repo_path: "../a9-v720/src"`.
4. Run `parking_monitor.py run --show`.

**Limitation:** while the PC's WiFi is joined to the camera, the PC has **no internet**, so ntfy and iCloud won't work. AP mode is only usable 24/7 if the PC gets internet from **Ethernet** and uses WiFi just for the camera.

### Firmware: when it won't work

a9-v720 was tested on firmware **202212011602**. Users report problems with firmware **≥ 20241120173** (newer cloud names, protocol changes). Check the version in the V720 app (device settings) or in the `Found A9 camera, FW version:` line in AP mode.

If your firmware is newer and neither 1B nor 1C produces frames, the plain answer is: **this camera isn't worth fighting**. Buy an RTSP camera, e.g. a **TP-Link Tapo C200/C110** (~USD 25, 1080p, IR night vision, real RTSP). Then:

1. In the Tapo app go to Camera → Settings → Advanced → Camera Account and create a username and password.
2. In `config.yaml` set:
   ```yaml
   source:
     type: rtsp
     rtsp:
       url: "rtsp://user:password@CAMERA_IP:554/stream1"
   ```

Nothing else changes. A 1080p camera also gives you about 3× the detection range (see below).

---

## Step 2 – Configure and run

Edit `config.yaml`: list your spot markers and names, set `reference_marker_id`, and set a **long random ntfy topic**.

```powershell
.venv\Scripts\python parking_monitor.py gen                 # markers/marker_XX.png + markers.pdf (30 cm)
.venv\Scripts\python parking_monitor.py gen --paper a4      # same, cut into A4 tiles to tape together
.venv\Scripts\python parking_monitor.py test-notify         # push test to your phone
.venv\Scripts\python parking_monitor.py run --show          # live debug window, q to quit
```

**How decisions are made:**

* A spot changes state only after its marker has been **continuously** missing (or visible) for `confirm_seconds` (default 10 s). A person walking over the marker doesn't trigger anything.
* If the **reference marker (ID 9)** is not visible, a missing spot marker is not trusted. The cause could be darkness, rain on the lens, fog, a spider, or the camera being offline. Spots are **never flipped to occupied** in that state. A spot marker that *is* seen can still mark its spot free, because seeing it is positive proof.
* A camera blind or offline condition lasting `camera_alert_seconds` (30 s) sends a push, and so does recovery. While blind, the JSON has `"camera_ok": false`.
* Your phone isn't spammed at startup. The first confirmed state is written to JSON but not pushed unless `notify_on_startup: true`.

**Status JSON** (`iCloudDrive\Parking\parking_status.json` on Windows, `~/Library/Mobile Documents/com~apple~CloudDocs/Parking/` on Mac):

```json
{
  "updated": "2026-10-12T22:34:17+08:00",
  "camera_ok": true,
  "camera_status": "ok",
  "free_count": 1,
  "total": 2,
  "summary": "1/2 free: Spot A",
  "spots": {
    "Spot A": {"marker_id": 1, "state": "free"},
    "Spot B": {"marker_id": 2, "state": "occupied"}
  }
}
```

The file is written to a temp file and then `os.replace`d over the real one, so readers never see a half-written file. It is rewritten on every change and at least every 5 min (heartbeat), so `updated` tells you the monitor is alive.
On Windows you need **iCloud for Windows** installed with iCloud Drive enabled. iCloud sync can lag by a minute or more; ntfy is instant.

Logs: `logs/parking.log` (1 MB × 5 files, rotating).

---

## Step 4 – Placement, marker size, printing

**Marker size vs distance – the most important number.**
ArUco needs the marker's *shortest* edge to be at least **~25 px** in the image; 40 px or more is comfortable. `--show` prints each marker's pixel size, so check it at night too.

```
pixels ≈ image_width_px × marker_m / (2 × distance_m × tan(HFOV/2)) × sin(viewing angle)
```

where the viewing angle is 90° when looking straight down and 30° for a shallow view.

| Camera | Marker | Max distance @ 45° view |
|---|---|---|
| A9, 640×480 stream, ~100° HFOV | 30 cm | ~2.3 m |
| A9, 640×480 | 45 cm | ~3.4 m |
| A9, 640×480 | 60 cm | ~4.6 m |
| Tapo C200, 1920×1080, ~104° | 30 cm | ~6.4 m |
| Tapo C200 | 45 cm | ~9.5 m |

The A9 typically streams only 640×480 through these tools. With 30 cm markers, mount it **close and high**, for example 2.5–3 m up on a wall and less than 2 m horizontally from the markers. For a longer reach, use bigger markers or an RTSP camera.

**Camera mounting**

* As high as possible, looking **down** at 45° or steeper. A steeper view means less foreshortening and less chance of a neighbouring car blocking the line of sight.
* Put the marker where the **car body always covers it**: the middle of the spot, roughly under the engine or between the axles. Not the edge, where a car parked off-centre might leave it visible.
* Avoid having the sun or streetlights in frame (lens flare). In IR night mode, avoid shiny surfaces close to the lens (IR bounce).
* **Reference marker (ID 9):** put it in the same light, at a similar distance, where nothing can ever park, such as a wall, kerb, or pole. When it disappears, the picture is unusable.

**Printing and mounting the markers**

* Print at **100 % / actual size**, never "fit to page". Measure the black square afterwards.
* Use **matte** media, because gloss reflects sun and IR into a white blob. Good options are matte vinyl, laminated with *matte* film, on a rigid board (aluminium composite / Dibond), screwed or glued flat to the ground. Outdoor road-marking paint through a laser-cut stencil is the most durable option.
* Keep the **white quiet zone** (printed automatically, one module wide). On dark asphalt it is what makes the black border detectable.
* A car drives over it, so keep it flat and low profile with no curling edges.
* **Cleaning:** dirt, leaves, snow, and tyre rubber on the white cells are the most common failure, and the reference marker can't catch them. Wipe the markers regularly. If one spot is "occupied" for an unusually long time, look at it with `--show`.

---

## ntfy on the iPhone

1. Install **ntfy** from the App Store (by Philipp Heckel).
2. Open it, tap **+**, enter the same `topic` as in `config.yaml`, and leave the server as `ntfy.sh`. Allow notifications.
3. On the PC, run `parking_monitor.py test-notify`. A push should arrive within seconds.

Topics on ntfy.sh are public, so anyone who guesses the name can read or post to it. Use something long and random (`parking-7f3k9q2xw…`). For privacy you can self-host ntfy and set `server` and `token`.

## Optional iOS Shortcut: "Parking"

Shortcuts app → **+**, then add these actions:

1. **Get File** – Service: iCloud Drive. Turn **off** "Show Document Picker". File Path: `Parking/parking_status.json`.
2. **Get Dictionary from Input**.
3. **Get Dictionary Value** – Key: `summary`.
4. **Show Result** (or *Speak Text*).

Name it "Parking". Now *"Hey Siri, Parking"* says e.g. "1/2 free: Spot A". You can also add it to the Home Screen or as a Shortcuts widget.

Optional extras:

* Read `camera_ok` with another *Get Dictionary Value* and use an **If** to warn when it is false.
* Compare `updated` to the current time to detect a stopped PC.

`summary` already includes "(camera blind)" when relevant.

> Note: the Shortcut shows `"Parking/..."` relative to the iCloud Drive root. If you see "file not found", tap the folder icon in *Get File* and browse to it once.

---

## Step 5 – Run on boot

* **Windows:** first create `.venv` (step 0), then run:
  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
  ```
  This starts the monitor at log on (iCloud only syncs while you're logged in) and restarts it every minute if it crashes. Set Windows to sign in automatically after reboot (netplwiz) if the PC must recover unattended.
* **macOS:** see `deploy/macos/com.parking.monitor.plist` (launchd LaunchAgent, KeepAlive).
* **Raspberry Pi:** see `deploy/linux/parking-monitor.service` (systemd). Linux has no iCloud client, so the JSON goes to `~/parking-status/`; ntfy works normally.

## Tests

```
python -m pytest -q
```

* `tests/test_state.py` – debounce, brief occlusion, car arrive/leave, camera blind never flips to occupied, blind time doesn't count toward confirm time, camera-health alert/recovery.
* `tests/test_synthetic.py` – markers rendered onto noisy "asphalt" with perspective and blur, a dark rectangle as the "car", a 25 %-brightness night frame (CLAHE), a full scenario through `Monitor` (JSON + notifications), offline camera, generated PNG round-trip, and the MJPEG byte parser.
* `tests/test_sources.py` – the MJPEG and A9 fake-server sources against a local HTTP server that mimics `/dev/list` + `/dev/<uid>/live`, and the file source.
