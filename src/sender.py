# Author: Nguyen Le Quang Anh (202414611) - HUST
"""
sender.py - Encode -> RTP packetize -> UDP transmit.

Usage:
    python sender.py <input_video> <dst_ip> <dst_port> [options]

Options:
    --bitrate KBPS      target video bitrate            (default 1000)
    --width  PX         output width                    (default 640)
    --height PX         output height                   (default 480)
    --fps    N          output frame rate               (default 30)
    --codec  h264|h265  video codec                     (default h264)
    --stream PATH       reuse a pre-encoded .h264/.hevc (skip encoding)
    --send-log PATH     write 'seq,send_time' CSV for delay measurement
    --meta PATH         write session JSON (frames, fps, codec, sent packets, ...)
    --encode-only       encode + write stream/meta, then exit (no transmission)

Pipeline
--------
1. Encode the source to an all-intra Annex-B stream (every frame an independent
   IDR with repeated SPS/PPS headers). Because there is no inter-frame
   prediction, losing one frame cannot corrupt any other frame, which keeps the
   loss experiments clean.
2. Split it into one access unit per frame.
3. For each frame: fragment into <=1400-byte payloads, wrap each in a 7-byte RTP
   header (seq_no, timestamp=frame index, marker=1 on the last fragment) and send
   over UDP, paced at 1/fps so the stream is emitted in real time.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import time

# ---------------------------------------------------------------- RTP layer
# 7-byte header: uint16 seq_no, uint32 timestamp (frame index), uint8 marker.
HEADER_FORMAT = ">HIB"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)   # 7 bytes
MAX_PAYLOAD = 1400                             # keep clear of the 1500 B MTU
EOS_TS = 0xFFFFFFFF                            # end-of-stream sentinel timestamp


def rtp_pack(seq_no, timestamp, marker, payload):
    header = struct.pack(HEADER_FORMAT, seq_no & 0xFFFF,
                         timestamp & 0xFFFFFFFF, marker & 0xFF)
    return header + payload


def fragment_frame(frame_bytes, max_payload=MAX_PAYLOAD):
    if not frame_bytes:
        return [b""]
    return [frame_bytes[i:i + max_payload]
            for i in range(0, len(frame_bytes), max_payload)]


# ---------------------------------------------------------------- encoder
START_CODE_4 = b"\x00\x00\x00\x01"
START_CODE_3 = b"\x00\x00\x01"


def encode_video(input_path, output_path, width, height, fps, bitrate_kbps, codec):
    """Encode the source into an all-intra Annex-B elementary stream."""
    if codec == "h264":
        vcodec, extra, out_fmt = "libx264", \
            ["-x264-params", "repeat-headers=1:keyint=1:scenecut=0"], "h264"
    elif codec == "h265":
        vcodec, extra, out_fmt = "libx265", \
            ["-x265-params", "repeat-headers=1:keyint=1:scenecut=0", "-tag:v", "hvc1"], "hevc"
    else:
        raise ValueError("codec must be 'h264' or 'h265'")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path, "-an",
        "-c:v", vcodec,
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-vf", f"scale={width}:{height},fps={fps}",
        "-g", "1", "-pix_fmt", "yuv420p",
    ] + extra + ["-f", out_fmt, output_path]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("FFmpeg encode failed:\n" + proc.stderr.decode(errors="replace"))
    return output_path


def split_access_units(stream_path):
    """Split an Annex-B stream into a list of per-frame access units."""
    with open(stream_path, "rb") as f:
        data = f.read()

    offsets, i, n = [], 0, len(data)
    while i < n - 3:
        if data[i:i + 4] == START_CODE_4:
            offsets.append((i, 4)); i += 4
        elif data[i:i + 3] == START_CODE_3:
            offsets.append((i, 3)); i += 3
        else:
            i += 1
    if not offsets:
        return [data] if data else []

    is_h265 = stream_path.endswith((".hevc", ".h265"))

    def nal_type(pos, sc_len):
        b = data[pos + sc_len]
        return (b >> 1) & 0x3F if is_h265 else b & 0x1F

    au_start_types = {32, 33} if is_h265 else {7}   # VPS/SPS (h265) or SPS (h264)
    boundaries = [pos for pos, sc_len in offsets if nal_type(pos, sc_len) in au_start_types]
    if not boundaries:
        boundaries = [pos for pos, _ in offsets]
    boundaries.append(len(data))

    units = []
    for k in range(len(boundaries) - 1):
        au = data[boundaries[k]:boundaries[k + 1]]
        if au:
            units.append(au)
    return units


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("dst_ip", nargs="?", default="127.0.0.1")
    ap.add_argument("dst_port", nargs="?", type=int, default=5000)
    ap.add_argument("--bitrate", type=int, default=1000)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--codec", choices=["h264", "h265"], default="h264")
    ap.add_argument("--stream", default=None, help="reuse pre-encoded elementary stream")
    ap.add_argument("--send-log", default=None)
    ap.add_argument("--meta", default=None)
    ap.add_argument("--start-delay", type=float, default=0.5)
    ap.add_argument("--encode-only", action="store_true")
    args = ap.parse_args()

    ext = "hevc" if args.codec == "h265" else "h264"

    # 1. Encode (or reuse a pre-encoded stream).
    if args.stream and os.path.exists(args.stream):
        stream_path = args.stream
    else:
        stream_path = args.stream or f"output/_send_{args.codec}_{args.bitrate}k.{ext}"
        os.makedirs(os.path.dirname(stream_path) or ".", exist_ok=True)
        encode_video(args.input, stream_path, args.width, args.height,
                     args.fps, args.bitrate, args.codec)

    # 2. Split into per-frame access units and count the packets we will send.
    aus = split_access_units(stream_path)
    n_frames = len(aus)
    stream_bytes = os.path.getsize(stream_path)
    planned_packets = sum(len(fragment_frame(au)) for au in aus)

    # Session description (sent out-of-band, like SDP).
    if args.meta:
        with open(args.meta, "w") as f:
            json.dump({
                "frames": n_frames, "fps": args.fps,
                "width": args.width, "height": args.height,
                "codec": args.codec, "bitrate_kbps": args.bitrate,
                "stream_path": stream_path, "stream_bytes": stream_bytes,
                "sent_packets": planned_packets,
            }, f)

    if args.encode_only:
        print(json.dumps({"encoded": stream_path, "frames": n_frames,
                          "stream_bytes": stream_bytes}))
        return

    # 3. Packetize and transmit, paced at the frame rate.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (args.dst_ip, args.dst_port)
    send_log = open(args.send_log, "w") if args.send_log else None

    time.sleep(args.start_delay)                 # give the receiver time to bind

    seq = 0
    frame_interval = 1.0 / args.fps
    t_start = time.time()
    total_payload = 0

    for frame_idx, au in enumerate(aus):
        deadline = t_start + frame_idx * frame_interval
        now = time.time()
        if deadline > now:
            time.sleep(deadline - now)

        fragments = fragment_frame(au)
        for j, payload in enumerate(fragments):
            marker = 1 if j == len(fragments) - 1 else 0
            sock.sendto(rtp_pack(seq, frame_idx, marker, payload), dst)
            if send_log:
                send_log.write(f"{seq},{time.time():.6f}\n")
            total_payload += len(payload)
            seq += 1

    # End-of-stream sentinel (sent a few times in case some are lost).
    for _ in range(3):
        sock.sendto(rtp_pack(seq, EOS_TS, 1, b"EOS"), dst)
        seq += 1
        time.sleep(0.01)

    if send_log:
        send_log.close()
    sock.close()

    print(json.dumps({
        "sent_packets": planned_packets,
        "frames": n_frames,
        "stream_bytes": stream_bytes,
        "payload_bytes": total_payload,
        "elapsed_s": round(time.time() - t_start, 3),
    }))


if __name__ == "__main__":
    main()
