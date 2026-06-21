# Author: Nguyen Le Quang Anh (202414611) - HUST
"""
app.py - Streamlit front-end for the Video Encoding and Streaming System.

Run with:
    streamlit run app.py

The app drives the three implementation modules in src/ (sender, network
emulator, receiver) as separate UDP processes, sweeps one network parameter over
several conditions, and renders the four required evaluation charts -
Throughput, Delay, Packet-loss rate and PSNR - together with the received video
for every condition.
"""

import json
import os
import random
import subprocess
import sys
import time

import matplotlib.pyplot as plt
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "output")
VID = os.path.join(OUT, "videos")
MET = os.path.join(OUT, "metrics")
ASSETS = os.path.join(ROOT, "assets")
for d in (VID, MET, ASSETS):
    os.makedirs(d, exist_ok=True)

PYEXE = sys.executable
HUST_RED = "#8b1e1e"
HUST_GOLD = "#c8a13a"
BLUE = "#1f6f8b"


# ----------------------------------------------------------------- helpers
def ffmpeg(args):
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, text=True)


def encode_stream(source, width, height, fps, bitrate, codec, tag):
    """Pre-encode the source once (sender --encode-only). Returns (stream, meta)."""
    ext = "hevc" if codec == "h265" else "h264"
    stream = os.path.join(OUT, f"_enc_{tag}.{ext}")
    meta = os.path.join(MET, f"sess_{tag}.json")
    subprocess.run([PYEXE, os.path.join(SRC, "sender.py"), source, "--encode-only",
                    "--bitrate", str(bitrate), "--width", str(width),
                    "--height", str(height), "--fps", str(fps), "--codec", codec,
                    "--stream", stream, "--meta", meta],
                   cwd=ROOT, capture_output=True, text=True)
    return stream, meta


def build_reference(source, width, height, fps, n_frames):
    """Near-lossless reference (same resolution/fps/frame-count) for PSNR."""
    ref = os.path.join(OUT, f"_reference_{width}x{height}_{fps}.mp4")
    ffmpeg(["-i", source, "-vf", f"scale={width}:{height},fps={fps}",
            "-frames:v", str(n_frames), "-c:v", "libx264", "-crf", "5",
            "-pix_fmt", "yuv420p", "-vsync", "passthrough", ref])
    return ref


def to_browser_mp4(in_path, tag):
    """Transcode an OpenCV-written video to H.264 so the browser can play it."""
    out = os.path.join(VID, f"play_{tag}.mp4")
    ffmpeg(["-i", in_path, "-c:v", "libx264", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
    return out if os.path.exists(out) else in_path


def run_condition(source, stream, meta, width, height, fps, bitrate, codec,
                  loss, delay, jitter, buffer_ms, reference, tag):
    """Run one sender -> emulator -> receiver pass. Returns the metrics dict."""
    base = random.randint(20000, 60000)
    em_port, rx_port = base, base + 1
    out_video = os.path.join(VID, f"recv_{tag}.mp4")
    metrics_json = os.path.join(MET, f"m_{tag}.json")
    send_log = os.path.join(MET, f"send_{tag}.csv")
    tmp = os.path.join(OUT, f"_recv_{tag}.{'hevc' if codec == 'h265' else 'h264'}")

    rx = subprocess.Popen(
        [PYEXE, os.path.join(SRC, "receiver.py"), str(rx_port), "--meta", meta,
         "--out", out_video, "--metrics", metrics_json, "--buffer-ms", str(buffer_ms),
         "--send-log", send_log, "--reference", reference, "--tmp", tmp,
         "--idle-timeout", "3"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.3)
    em = subprocess.Popen(
        [PYEXE, os.path.join(SRC, "network_emulator.py"), str(em_port), "127.0.0.1",
         str(rx_port), "--loss", str(loss), "--delay", str(delay),
         "--jitter", str(jitter), "--idle-timeout", "3"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.3)
    tx = subprocess.Popen(
        [PYEXE, os.path.join(SRC, "sender.py"), source, "127.0.0.1", str(em_port),
         "--bitrate", str(bitrate), "--width", str(width), "--height", str(height),
         "--fps", str(fps), "--codec", codec, "--stream", stream, "--meta", meta,
         "--send-log", send_log],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        tx.communicate(timeout=120)
        rx.communicate(timeout=120)
        em.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        for p in (tx, rx, em):
            p.kill()

    with open(metrics_json) as f:
        m = json.load(f)
    if m.get("out_video") and os.path.exists(m["out_video"]):
        m["play_video"] = to_browser_mp4(m["out_video"], tag)
    return m


def line_chart(x, y, xlabel, ylabel, title, color, fmt="{:.1f}"):
    fig, ax = plt.subplots(figsize=(5, 3.2))
    ax.plot(x, y, "-o", color=color, linewidth=2, markersize=7)
    for xi, yi in zip(x, y):
        if yi is not None:
            ax.annotate(fmt.format(yi), (xi, yi), textcoords="offset points",
                        xytext=(0, 8), ha="center", fontsize=8, color="#444")
    ax.set_xlabel(xlabel); ax.set_ylabel(ylabel)
    ax.set_title(title, color=HUST_RED, fontweight="bold")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig


# ----------------------------------------------------------------- UI
st.set_page_config(page_title="Video Streaming System", layout="wide")
st.title("Video Encoding and Streaming System")
st.caption("Exercise 4 - Encoding and Streaming  |  Nguyen Le Quang Anh (202414611) - HUST")
st.write(
    "Pipeline: **Encoder -> RTP Sender -> UDP -> Network Emulator -> RTP Receiver "
    "-> Jitter Buffer -> Decoder**. Choose a parameter to sweep, run the stream, "
    "and inspect throughput, delay, packet-loss rate and PSNR plus the received video "
    "for each condition."
)

with st.sidebar:
    st.header("Source video")
    uploaded = st.file_uploader("Upload a video (optional)", type=["mp4", "avi", "mov", "mkv"])
    if uploaded is not None:
        source = os.path.join(ASSETS, "uploaded_" + uploaded.name)
        with open(source, "wb") as f:
            f.write(uploaded.read())
    else:
        source = os.path.join(ASSETS, "test_source.mp4")
        st.caption("Using the bundled synthetic test clip.")

    st.header("Encoding")
    codec = st.selectbox("Codec", ["h264", "h265"], index=0)
    res = st.selectbox("Resolution", ["640x480", "854x480", "1280x720"], index=0)
    width, height = (int(v) for v in res.split("x"))
    fps = st.selectbox("Frame rate", [24, 30], index=1)

    st.header("Experiment")
    sweep = st.selectbox("Sweep variable",
                         ["Packet loss (%)", "Bitrate (kbps)", "Jitter buffer (ms)"])
    st.caption("Other parameters are held fixed at the values below.")
    base_bitrate = st.select_slider("Bitrate (kbps)", [500, 1000, 2000, 4000], value=2000)
    base_delay = st.slider("Base delay (ms)", 0, 200, 30, 10)
    base_jitter = st.slider("Jitter (ms)", 0, 120, 0, 10)
    base_buffer = st.select_slider("Jitter buffer (ms)", [0, 50, 100, 200], value=100)
    run = st.button("Run streaming", type="primary")

# Sweep definitions: (list of values, label, fixed-context note)
if sweep.startswith("Packet loss"):
    values = [0, 5, 10, 20]; xlabel = "Packet loss (%)"
elif sweep.startswith("Bitrate"):
    values = [500, 1000, 2000, 4000]; xlabel = "Bitrate (kbps)"
else:
    values = [0, 50, 100, 200]; xlabel = "Jitter buffer (ms)"
    if base_jitter == 0:
        base_jitter = 60  # a buffer sweep only matters when there is jitter to absorb

if run:
    conditions = []
    progress = st.progress(0.0, text="Preparing...")

    # Pre-encode streams. For bitrate sweeps we encode one stream per bitrate;
    # otherwise a single stream is reused across all conditions.
    cache = {}

    def stream_for(bitrate):
        if bitrate not in cache:
            s, m = encode_stream(source, width, height, fps, bitrate, codec, f"b{bitrate}")
            n = json.load(open(m))["frames"]
            ref = build_reference(source, width, height, fps, n)
            cache[bitrate] = (s, m, ref)
        return cache[bitrate]

    for i, v in enumerate(values):
        if sweep.startswith("Packet loss"):
            bitrate, loss, jitter, buffer_ms = base_bitrate, v, base_jitter, base_buffer
        elif sweep.startswith("Bitrate"):
            bitrate, loss, jitter, buffer_ms = v, 0, base_jitter, base_buffer
        else:
            bitrate, loss, jitter, buffer_ms = base_bitrate, 0, base_jitter, v

        stream, meta, ref = stream_for(bitrate)
        progress.progress(i / len(values), text=f"Streaming condition {i+1}/{len(values)} ({xlabel.split(' (')[0]} = {v})")
        m = run_condition(source, stream, meta, width, height, fps, bitrate, codec,
                          loss, base_delay, jitter, buffer_ms, ref, tag=f"{sweep[:4]}_{v}")
        m["_x"] = v
        conditions.append(m)

    progress.progress(1.0, text="Done")
    st.session_state["results"] = {"xlabel": xlabel, "conditions": conditions}

# ----------------------------------------------------------------- results
res_state = st.session_state.get("results")
if res_state:
    xlabel = res_state["xlabel"]
    conds = res_state["conditions"]
    xs = [c["_x"] for c in conds]
    throughput = [c.get("throughput_mbps") for c in conds]
    delay = [c.get("delay_ms", {}).get("mean") for c in conds]
    loss = [c.get("loss_rate_pct") for c in conds]
    psnr = [c.get("psnr_db") for c in conds]

    st.subheader("Evaluation charts")
    c1, c2 = st.columns(2)
    with c1:
        st.pyplot(line_chart(xs, throughput, xlabel, "Mbps", "Throughput", HUST_GOLD, "{:.2f}"))
        st.pyplot(line_chart(xs, loss, xlabel, "%", "Packet loss rate", HUST_RED, "{:.1f}"))
    with c2:
        st.pyplot(line_chart(xs, delay, xlabel, "ms", "End-to-end delay", BLUE, "{:.0f}"))
        st.pyplot(line_chart(xs, psnr, xlabel, "dB", "PSNR", "#2e7d32", "{:.1f}"))

    st.subheader("Metrics table")
    table = []
    for c in conds:
        table.append({
            xlabel: c["_x"],
            "Throughput (Mbps)": c.get("throughput_mbps"),
            "Delay mean (ms)": c.get("delay_ms", {}).get("mean"),
            "Loss (%)": c.get("loss_rate_pct"),
            "PSNR (dB)": c.get("psnr_db"),
            "Frames decoded": f'{c.get("frames_decoded")}/{c.get("frames_total")}',
        })
    st.dataframe(table, hide_index=True)

    st.subheader("Received videos")
    cols = st.columns(min(len(conds), 4))
    for col, c in zip(cols, conds):
        with col:
            st.markdown(f"**{xlabel.split(' (')[0]} = {c['_x']}**")
            vid = c.get("play_video") or c.get("out_video")
            if vid and os.path.exists(vid):
                st.video(vid)
            else:
                st.write("No video produced.")
            st.caption(f"PSNR {c.get('psnr_db')} dB | decoded "
                       f"{c.get('frames_decoded')}/{c.get('frames_total')} frames")
else:
    st.info("Configure the experiment in the sidebar and click **Run streaming**.")
