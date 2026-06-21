# Author: Nguyen Le Quang Anh (202414611) - HUST
"""
receiver.py - Receive -> jitter buffer -> reassemble -> decode -> measure.

Usage:
    python receiver.py <listen_port> --meta session.json --out received.mp4 \
           --metrics metrics.json [--buffer-ms 100] [--send-log send.csv] \
           [--reference ref.mp4]

Pipeline
--------
1. Receive RTP packets on a UDP port, recording each packet's arrival time,
   sequence number, frame index (timestamp) and marker.
2. Jitter buffer: playback starts `buffer_ms` after the first packet. Each frame
   has a playout deadline; fragments arriving after their frame's deadline are
   "late" and discarded. A larger buffer absorbs more jitter at the cost of
   added latency.
3. Reassemble each frame from its fragments (grouped by frame index, ordered by
   sequence number). A frame is good only if every fragment arrived in time and
   none is missing; otherwise the previous good frame is repeated (freeze-frame
   concealment), keeping the output time-aligned with the source.
4. Decode the good frames, assemble the full N-frame video, and compute the four
   evaluation metrics: throughput, packet-loss rate, end-to-end delay and PSNR.
"""

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import time

# ---------------------------------------------------------------- RTP layer
import struct
HEADER_FORMAT = ">HIB"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
EOS_TS = 0xFFFFFFFF


def rtp_unpack(packet):
    seq_no, timestamp, marker = struct.unpack(HEADER_FORMAT, packet[:HEADER_SIZE])
    return seq_no, timestamp, marker, packet[HEADER_SIZE:]


# ---------------------------------------------------------------- decoding
def decode_au_stream_to_frames(stream_path, ext, width, height):
    """Decode an Annex-B stream of access units into a list of BGR frames."""
    import numpy as np
    fmt = "hevc" if ext in ("hevc", "h265") else "h264"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", fmt, "-r", "30", "-i", stream_path,
        "-vf", f"scale={width}:{height}", "-vsync", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    raw = proc.stdout
    frame_size = width * height * 3
    count = len(raw) // frame_size
    return [np.frombuffer(raw[i * frame_size:(i + 1) * frame_size], dtype=np.uint8)
            .reshape(height, width, 3) for i in range(count)]


def write_frames_to_video(frames, out_path, fps):
    import cv2
    if not frames:
        return False
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for fr in frames:
        vw.write(fr)
    vw.release()
    return True


# ---------------------------------------------------------------- metrics
def compute_psnr(reference_video, received_video):
    """Average PSNR (dB) via FFmpeg's psnr filter (inf -> capped at 100)."""
    if not reference_video or not os.path.exists(reference_video):
        return None
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info",
           "-i", received_video, "-i", reference_video,
           "-lavfi", "psnr", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    m = re.search(r"average:([0-9.]+|inf)", out)
    if not m:
        return None
    v = m.group(1)
    return 100.0 if v == "inf" else float(v)


def load_log(path):
    d = {}
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    seq, t = line.split(",")
                    d[int(seq)] = float(t)
    return d


def delay_stats(send_log_path, recv_log):
    """Join send/recv times on seq_no -> mean/p95/max/jitter delay in ms."""
    sent = load_log(send_log_path)
    delays = [(recv_log[s] - sent[s]) * 1000.0 for s in recv_log if s in sent]
    delays = [d for d in delays if d >= 0]
    if not delays:
        return {"mean": 0.0, "p95": 0.0, "max": 0.0, "jitter": 0.0}, len(sent)
    delays.sort()
    p95 = delays[min(len(delays) - 1, int(len(delays) * 0.95))]
    return ({"mean": round(statistics.mean(delays), 2),
             "p95": round(p95, 2),
             "max": round(max(delays), 2),
             "jitter": round(statistics.pstdev(delays) if len(delays) > 1 else 0.0, 2)},
            len(sent))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("listen_port", type=int)
    ap.add_argument("--meta", required=True, help="session JSON from sender")
    ap.add_argument("--out", required=True, help="output decoded video path")
    ap.add_argument("--metrics", required=True, help="output metrics JSON path")
    ap.add_argument("--buffer-ms", type=float, default=100.0)
    ap.add_argument("--send-log", default=None, help="sender CSV for delay + loss")
    ap.add_argument("--reference", default=None, help="reference video for PSNR")
    ap.add_argument("--recv-log", default=None)
    ap.add_argument("--idle-timeout", type=float, default=4.0)
    ap.add_argument("--tmp", default="output/_recv_stream.h264")
    args = ap.parse_args()

    # The sender writes the session meta; if the receiver starts first, wait.
    wait_until = time.time() + 10.0
    while not os.path.exists(args.meta) and time.time() < wait_until:
        time.sleep(0.1)
    with open(args.meta) as f:
        meta = json.load(f)
    n_frames = meta["frames"]
    fps = meta["fps"]
    width, height = meta["width"], meta["height"]
    codec = meta["codec"]
    ext = "hevc" if codec == "h265" else "h264"
    tmp = args.tmp if args.tmp.endswith((".h264", ".hevc")) else args.tmp + "." + ext
    os.makedirs(os.path.dirname(tmp) or ".", exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.listen_port))
    sock.settimeout(0.5)

    packets = {}                 # frame_idx -> [(seq, marker, payload, arrival_t)]
    recv_log = {}
    received_pkts = 0
    payload_bytes = 0
    first_arrival = last_arrival = None
    got_eos = False
    last_activity = time.time()

    while True:
        try:
            pkt, _ = sock.recvfrom(65535)
        except socket.timeout:
            if first_arrival is not None and (time.time() - last_activity) > args.idle_timeout:
                break
            continue

        now = time.time()
        seq, ts, marker, payload = rtp_unpack(pkt)
        last_activity = now

        if ts == EOS_TS:
            got_eos = True
            continue

        if first_arrival is None:
            first_arrival = now
        last_arrival = now
        received_pkts += 1
        payload_bytes += len(payload)
        recv_log[seq] = now
        packets.setdefault(ts, []).append((seq, marker, payload, now))

        if got_eos and (now - last_activity) > 1.0:
            break

    sock.close()

    if first_arrival is None:
        result = {"error": "no packets received"}
        with open(args.metrics, "w") as f:
            json.dump(result, f)
        print(json.dumps(result))
        return

    # ---- Jitter-buffer playout model ----
    frame_interval = 1.0 / fps
    playout_start = first_arrival + args.buffer_ms / 1000.0

    good_au = [None] * n_frames
    reason = [None] * n_frames
    for k in range(n_frames):
        deadline = playout_start + k * frame_interval
        frags = packets.get(k)
        if not frags:
            reason[k] = "loss"
            continue
        frags.sort(key=lambda x: x[0])
        seqs = [s for s, _, _, _ in frags]
        has_marker = any(m == 1 for _, m, _, _ in frags)
        contiguous = (max(seqs) - min(seqs) + 1) == len(seqs)
        latest = max(a for _, _, _, a in frags)
        if not (has_marker and contiguous):
            reason[k] = "loss"
        elif latest > deadline:
            reason[k] = "late"
        else:
            good_au[k] = b"".join(p for _, _, p, _ in frags)

    good_indices = [k for k in range(n_frames) if good_au[k] is not None]
    decoded_ok = len(good_indices)
    lost_network = sum(1 for k in range(n_frames)
                       if good_au[k] is None and reason[k] != "late")
    late_dropped = sum(1 for k in range(n_frames) if reason[k] == "late")
    frozen = n_frames - decoded_ok

    # Decode only the good frames, then assemble the full N-frame video using
    # image-level freeze-frame concealment (repeat the last good image).
    good_frames = []
    if good_indices:
        with open(tmp, "wb") as f:
            f.write(b"".join(good_au[k] for k in good_indices))
        good_frames = decode_au_stream_to_frames(tmp, ext, width, height)

    import numpy as np
    black = np.zeros((height, width, 3), dtype=np.uint8)
    first_good_img = good_frames[0] if good_frames else black
    idx_to_img = {k: good_frames[pos] for pos, k in enumerate(good_indices)
                  if pos < len(good_frames)}

    seq_frames, last_img = [], None
    for k in range(n_frames):
        if k in idx_to_img:
            img = idx_to_img[k]
        elif last_img is not None:
            img = last_img
        else:
            img = first_good_img
        seq_frames.append(img)
        last_img = img

    decode_ok = write_frames_to_video(seq_frames, args.out, fps)

    # ---- The four required metrics ----
    duration = max(1e-6, last_arrival - first_arrival)
    throughput_mbps = round((payload_bytes * 8) / duration / 1e6, 4)

    delay, sent_from_log = delay_stats(args.send_log, recv_log)
    sent_packets = sent_from_log or meta.get("sent_packets", received_pkts)
    loss_rate_pct = round(max(0.0, (sent_packets - received_pkts) / sent_packets * 100.0), 2) \
        if sent_packets else 0.0

    psnr = compute_psnr(args.reference, args.out) if decode_ok else None

    result = {
        "buffer_ms": args.buffer_ms,
        "throughput_mbps": throughput_mbps,
        "delay_ms": delay,
        "loss_rate_pct": loss_rate_pct,
        "psnr_db": round(psnr, 2) if psnr is not None else None,
        "sent_packets": sent_packets,
        "received_packets": received_pkts,
        "frames_total": n_frames,
        "frames_decoded": decoded_ok,
        "frames_lost_network": lost_network,
        "frames_late": late_dropped,
        "frames_frozen": frozen,
        "smoothness_pct": round(100.0 * decoded_ok / max(1, n_frames), 2),
        "out_video": args.out if decode_ok else None,
    }

    if args.recv_log:
        with open(args.recv_log, "w") as f:
            for seq, t in sorted(recv_log.items()):
                f.write(f"{seq},{t:.6f}\n")

    with open(args.metrics, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
