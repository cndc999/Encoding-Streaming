# Video Encoding and Streaming System

**Exercise 4 - Encoding and Streaming**
**Author:** Nguyen Le Quang Anh (Student ID: 202414611)
**Hanoi University of Science and Technology (HUST)**

A simplified real-time video streaming system with a Streamlit interface. The full
pipeline is:

```
Video Source -> Encoder -> RTP Sender -> UDP -> Network Emulator -> RTP Receiver -> Jitter Buffer -> Decoder -> Video Output
```

A video is encoded with H.264/H.265, packetized into simplified RTP packets, streamed
over UDP through a configurable network emulator (loss / delay / jitter / reorder), then
reassembled, buffered, decoded and re-displayed at the receiver. The app sweeps one
network parameter and plots the four required metrics - **Throughput, Delay, Packet-loss
rate and PSNR** - and shows the received video for every condition.

---

## 1. Project layout

The only file outside `src/` is `app.py` (the Streamlit front-end). All implementation
code lives in `src/` and is exactly three modules - the Sender, the Receiver and the
Network emulator.

```
streaming_app/
  app.py                      # Streamlit UI: runs the pipeline, draws the charts
  requirements.txt
  README.md
  src/
    sender.py                 # encode (H.264/H.265) + RTP packetize + UDP send
    receiver.py               # UDP receive + jitter buffer + decode + 4 metrics
    network_emulator.py       # UDP proxy: loss / delay / jitter / reorder
  assets/
    test_source.mp4           # bundled synthetic test clip
  output/
    videos/                   # received videos (one per condition)
    metrics/                  # metrics JSON + send/recv timing logs
```

---

## 2. Requirements

- Python 3.10+
- `ffmpeg` / `ffprobe` built with `libx264` and `libx265` (system dependency, not pip-installable)
- Python packages in `requirements.txt` (`streamlit`, `numpy`, `opencv-python`, `matplotlib`)

```bash
sudo apt install ffmpeg
pip install -r requirements.txt
```

---

## 3. Running the app

```bash
streamlit run app.py
```

This opens the interface in the browser. In the sidebar:

1. Choose a source video (upload one, or use the bundled test clip).
2. Set the encoding options (codec, resolution, frame rate).
3. Pick the **sweep variable** - Packet loss, Bitrate, or Jitter buffer - and the fixed
   values for the other parameters.
4. Click **Run streaming**.

The app streams the video once for each condition and then displays:

- Four charts: **Throughput**, **End-to-end delay**, **Packet-loss rate**, **PSNR**.
- A metrics table for every condition.
- The **received video** for each condition (re-encoded to H.264 for in-browser playback).

All received videos and metrics are also written to `output/videos/` and `output/metrics/`.

---

## 4. The implementation modules (`src/`)

Each module is self-contained and can also be run on its own from the command line.

### 4.1 `src/sender.py` - Encoder + RTP Sender
Encodes the source in **all-intra mode** (every frame an independently decodable IDR with
repeated SPS/PPS headers), so losing one frame cannot corrupt any other frame. Each frame
(access unit) is fragmented into <=1400-byte RTP packets. The 7-byte header (`>HIB`)
carries the three fields required by the brief: `seq_no` (uint16), `timestamp` (uint32,
the frame index) and `marker` (uint8, set on the last fragment of a frame). Packets are
paced at 1/fps.

```bash
python src/sender.py <input.mp4> <dst_ip> <dst_port> --bitrate 2000 --codec h264 \
                     --meta session.json --send-log send.csv
```

### 4.2 `src/network_emulator.py` - Network Emulator
A UDP proxy between sender and receiver. Per packet it independently applies loss, a fixed
delay, random jitter and optional reordering.

```bash
python src/network_emulator.py <listen_port> <dst_ip> <dst_port> \
                               --loss 10 --delay 30 --jitter 20
```

### 4.3 `src/receiver.py` - Receiver + Jitter Buffer + Metrics
Runs a jitter-buffer playout model (playback starts `buffer_ms` after the first packet;
each frame has a deadline). Good frames are decoded and the full video is reassembled with
freeze-frame concealment so it stays time-aligned with the source. It then computes the
four metrics:

- **Throughput** - received payload bytes / reception duration (Mbps)
- **Packet-loss rate** - `1 - received/sent` packets (%)
- **End-to-end delay** - `recv_time - send_time` joined on `seq_no` (mean / p95 / max / jitter)
- **PSNR** - received video vs a near-lossless reference (ffmpeg `psnr` filter)

```bash
python src/receiver.py <listen_port> --meta session.json --out received.mp4 \
                       --metrics m.json --buffer-ms 100 --send-log send.csv \
                       --reference reference.mp4
```

---

## 5. Manual single-stream run (without the UI)

Open three terminals (receiver -> emulator -> sender):

```bash
# 1) Receiver
python src/receiver.py 6000 --meta session.json --out received.mp4 --metrics m.json \
                       --buffer-ms 100 --send-log send.csv --reference assets/test_source.mp4

# 2) Emulator (forward 5000 -> 6000)
python src/network_emulator.py 5000 127.0.0.1 6000 --loss 10 --delay 30 --jitter 20

# 3) Sender
python src/sender.py assets/test_source.mp4 127.0.0.1 5000 --bitrate 2000 --codec h264 \
                     --meta session.json --send-log send.csv
```
