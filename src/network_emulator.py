# Author: Nguyen Le Quang Anh (202414611) - HUST
"""
network_emulator.py - Network impairment proxy between sender and receiver.

Sits on a UDP port, receives packets from the sender and forwards them to the
receiver after applying:

    Packet loss   : drop a configurable fraction of packets      (--loss %)
    Delay         : fixed base one-way delay                      (--delay ms)
    Jitter        : extra uniform random delay 0..J ms            (--jitter ms)
    Reordering    : naturally produced by per-packet jitter, plus an optional
                    explicit reorder probability                  (--reorder %)

Usage:
    python network_emulator.py <listen_port> <dst_ip> <dst_port> [options]

Each forwarded packet is scheduled on a worker thread that sleeps for its
computed delay, so packets whose delays cross over arrive reordered - exactly
the condition a jitter buffer is meant to absorb.
"""

import argparse
import json
import random
import socket
import threading
import time


def run(listen_port, dst_ip, dst_port, loss=0.0, delay=0.0, jitter=0.0,
        reorder=0.0, seed=12345, idle_timeout=3.0):
    """Run the emulator until the stream goes idle. Returns a stats dict."""
    rng = random.Random(seed)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("0.0.0.0", listen_port))
    rx.settimeout(0.5)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (dst_ip, dst_port)

    stats = {"received": 0, "dropped": 0, "forwarded": 0}
    lock = threading.Lock()

    def forward_later(pkt, delay_s):
        if delay_s > 0:
            time.sleep(delay_s)
        tx.sendto(pkt, dst)
        with lock:
            stats["forwarded"] += 1

    threads = []
    last_activity = time.time()
    started = False

    while True:
        try:
            pkt, _ = rx.recvfrom(65535)
        except socket.timeout:
            if started and (time.time() - last_activity) > idle_timeout:
                break
            continue

        started = True
        last_activity = time.time()
        with lock:
            stats["received"] += 1

        # Packet loss.
        if rng.random() * 100.0 < loss:
            with lock:
                stats["dropped"] += 1
            continue

        # Delay + jitter.
        delay_s = delay / 1000.0
        if jitter > 0:
            delay_s += rng.uniform(0, jitter) / 1000.0
        # Explicit extra reordering: occasionally add a large random hop.
        if reorder > 0 and rng.random() * 100.0 < reorder:
            delay_s += rng.uniform(0, max(jitter, 30)) / 1000.0

        th = threading.Thread(target=forward_later, args=(pkt, delay_s), daemon=True)
        th.start()
        threads.append(th)

    # Let in-flight scheduled packets drain.
    time.sleep((delay + jitter) / 1000.0 + 0.5)
    for th in threads:
        th.join(timeout=0.1)

    rx.close()
    tx.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("listen_port", type=int)
    ap.add_argument("dst_ip")
    ap.add_argument("dst_port", type=int)
    ap.add_argument("--loss", type=float, default=0.0, help="loss rate %")
    ap.add_argument("--delay", type=float, default=0.0, help="base delay ms")
    ap.add_argument("--jitter", type=float, default=0.0, help="max extra jitter ms")
    ap.add_argument("--reorder", type=float, default=0.0, help="extra reorder prob %")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--idle-timeout", type=float, default=3.0)
    args = ap.parse_args()

    stats = run(args.listen_port, args.dst_ip, args.dst_port,
                args.loss, args.delay, args.jitter, args.reorder,
                args.seed, args.idle_timeout)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
