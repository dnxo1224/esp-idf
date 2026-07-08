#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
CSI CSV 사후 분석 / 시각화
─────────────────────────────────────────────
multi_rx_collect_csi_v3.py 로 저장한 8개 Rx CSV를 한 번에 읽어서
수신 품질을 진단하고 그림으로 저장한다.

이 데이터의 CSV 포맷 (헤더 없음, 콤마 구분):
  col[0]   = 'CSI_DATA'
  col[2]   = seq   (송신 시퀀스 번호, 정상이면 1씩 증가)  ← 유실 추적 핵심
  col[4]   = rssi
  col[25..]= CSI raw I/Q 배열 (따옴표로 묶인 "[v v v ...]" 가 공백 기준으로
             여러 컬럼에 쪼개져 들어옴)
  col[-1]  = 수집기가 붙인 절대 수신시각(epoch, float)

진단 핵심:
  여러 Rx가 '같은 seq'를 동시에 놓쳤는가?
    - 동시 유실 多  → 송신(AP)/무선 채널 문제
    - 단독 유실 多  → 각 Rx의 USB/시리얼 로컬 병목

산출물 (--outdir, 기본 현재폴더):
  csi_summary.png      : Rx별 유실률 막대 + 동시유실 분포
  csi_loss_map.png     : Rx×seq 수신맵 + 시간축 수신율 + 동시유실
  csi_heatmap_<Rx>.png : 선택 Rx의 CSI 진폭 히트맵
  console              : Rx별 통계 표 + 자동 원인 판정
"""

import os
import csv
import glob
import argparse
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams


# ── 한글 폰트 설정 ────────────────────────────
def setup_korean_font():
    for name in ("Malgun Gothic", "AppleGothic", "NanumGothic", "NanumBarunGothic"):
        if any(name in f.name for f in font_manager.fontManager.ttflist):
            rcParams["font.family"] = name
            rcParams["axes.unicode_minus"] = False
            return name
    rcParams["axes.unicode_minus"] = False
    print("[!] 한글 폰트를 못 찾았습니다. 라벨이 깨질 수 있어요 (Malgun Gothic 등 설치 권장).")
    return None


SEQ_COL = 2
RSSI_COL = 4
CSI_START = 25
WRAP = 65536  # seq 16비트 wraparound


# ──────────────────────────────────────────────
# 파일 한 개 파싱
# ──────────────────────────────────────────────
def load_file(path):
    """seq, rssi, amp(list of np.array), broken 행 수 반환"""
    seqs, rssis, amps = [], [], []
    broken = 0
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0] != "CSI_DATA" or len(row) < 28:
                broken += 1
                continue
            # 두 프레임이 한 줄에 붙은 깨진 행: col[8]에 또 CSI_DATA
            if len(row) > 8 and row[8] == "CSI_DATA":
                broken += 1
                continue
            try:
                seq = int(row[SEQ_COL])
                rssi = int(row[RSSI_COL])
            except (ValueError, IndexError):
                broken += 1
                continue
            if rssi > 0 or rssi < -100:
                broken += 1
                continue

            # CSI raw: col[25]부터 끝-1(마지막은 수집기 절대시각)까지
            raw = []
            for c in row[CSI_START:-1]:
                tok = c.strip().strip('"').strip("[]")
                if tok == "":
                    continue
                try:
                    raw.append(int(tok))
                except ValueError:
                    pass
            if len(raw) >= 2:
                iq = np.array(raw[: (len(raw) // 2) * 2], dtype=np.float32).reshape(-1, 2)
                amp = np.sqrt(iq[:, 0] ** 2 + iq[:, 1] ** 2)
            else:
                amp = np.array([], dtype=np.float32)

            seqs.append(seq)
            rssis.append(rssi)
            amps.append(amp)

    return np.array(seqs), np.array(rssis), amps, broken


# ──────────────────────────────────────────────
# 데이터셋 전체 로드
# ──────────────────────────────────────────────
def load_dataset(files):
    data = {}
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        # 파일명 끝의 rxN 추출
        tag = name.split("_")[-1]
        label = tag.upper() if tag.lower().startswith("rx") else name
        seqs, rssis, amps, broken = load_file(path)
        if len(seqs) == 0:
            print(f"[!] {name}: 유효 행 없음, 건너뜀")
            continue
        data[label] = {"seq": seqs, "rssi": rssis, "amp": amps,
                       "broken": broken, "file": os.path.basename(path)}
    return data


# ──────────────────────────────────────────────
# Rx별 통계 + 공통 seq 매트릭스
# ──────────────────────────────────────────────
def compute_stats(data):
    labels = sorted(data.keys(), key=lambda x: (len(x), x))
    all_start = min(data[k]["seq"][0] for k in labels)
    all_end = max(data[k]["seq"][-1] for k in labels)
    span = all_end - all_start + 1

    mat = np.zeros((len(labels), span), dtype=np.int8)  # 1=수신,0=유실
    stats = []
    for i, k in enumerate(labels):
        seqs = data[k]["seq"]
        rel = seqs - all_start
        rel = rel[(rel >= 0) & (rel < span)]
        mat[i, rel] = 1

        expected = seqs[-1] - seqs[0] + 1
        received = len(seqs)
        lost = expected - received
        rate = 100 * lost / expected if expected > 0 else 0
        stats.append({
            "label": k, "received": received, "lost": lost,
            "loss_rate": rate, "broken": data[k]["broken"],
            "rssi_mean": float(np.mean(data[k]["rssi"])),
        })
    return labels, mat, span, all_start, stats


# ──────────────────────────────────────────────
# 그림 1: 요약 (막대 + 동시유실 분포)
# ──────────────────────────────────────────────
def plot_summary(labels, mat, stats, outpath):
    n = len(labels)
    lost_per_seq = n - mat.sum(axis=0)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # 좌: Rx별 유실률
    rates = [s["loss_rate"] for s in stats]
    colors = ["#2ca02c" if r < 8 else "#ff7f0e" if r < 13 else "#d62728" for r in rates]
    bars = ax1.bar(labels, rates, color=colors)
    for b, r in zip(bars, rates):
        ax1.text(b.get_x() + b.get_width() / 2, r + 0.2, f"{r:.1f}%",
                 ha="center", va="bottom", fontsize=9)
    ax1.set_ylabel("유실률 (%)")
    ax1.set_title("Rx별 프레임 유실률", fontweight="bold")
    ax1.axhline(np.mean(rates), color="gray", ls="--", lw=0.8,
                label=f"평균 {np.mean(rates):.1f}%")
    ax1.legend(); ax1.grid(axis="y", alpha=0.3)

    # 우: 동시 유실 분포
    span = mat.shape[1]
    dist = [int((lost_per_seq == k).sum()) for k in range(n + 1)]
    pcts = [100 * d / span for d in dist]
    bars = ax2.bar(range(n + 1), pcts, color="crimson", alpha=0.7)
    for b, p, d in zip(bars, pcts, dist):
        if p > 0.3:
            ax2.text(b.get_x() + b.get_width() / 2, p + 0.3, f"{p:.1f}%",
                     ha="center", va="bottom", fontsize=8)
    ax2.set_xlabel("동시에 같은 프레임을 놓친 Rx 수")
    ax2.set_ylabel("해당 비율 (%)")
    ax2.set_title("동시 유실 분포\n(1쪽=USB/로컬 병목, n쪽=AP/무선 문제)", fontweight="bold")
    ax2.set_xticks(range(n + 1))
    ax2.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(outpath, dpi=110, bbox_inches="tight")
    plt.close()
    return lost_per_seq


# ──────────────────────────────────────────────
# 그림 2: 유실 맵 (시간축)
# ──────────────────────────────────────────────
def plot_loss_map(labels, mat, span, lost_per_seq, outpath):
    n = len(labels)
    fig, axes = plt.subplots(3, 1, figsize=(15, 11),
                             gridspec_kw={"height_ratios": [2, 1.3, 1.3]})

    ax = axes[0]
    ax.imshow(mat, aspect="auto", cmap="RdYlGn", interpolation="nearest",
              vmin=0, vmax=1)
    ax.set_yticks(range(n)); ax.set_yticklabels(labels)
    ax.set_xlabel("시퀀스 번호 (상대)   —   빨강 = 유실 프레임")
    ax.set_title(f"Rx별 수신 맵  (초록=수신, 빨강=유실)   [전체 {span} 프레임]",
                 fontweight="bold")

    ax = axes[1]
    win = 100
    kern = np.ones(win) / win
    for i in range(n):
        rate = np.convolve(mat[i], kern, mode="valid") * 100
        ax.plot(rate, lw=0.8, label=labels[i])
    ax.set_ylabel(f"수신율 % (이동평균 {win})")
    ax.set_xlabel("시퀀스 번호")
    ax.set_title("시간에 따른 Rx별 수신율", fontweight="bold")
    ax.legend(ncol=n, fontsize=8, loc="lower center")
    ax.set_ylim(0, 105); ax.grid(alpha=0.3)

    ax = axes[2]
    ax.fill_between(range(span), lost_per_seq, step="mid",
                    color="crimson", alpha=0.6)
    ax.set_ylabel("동시 유실 Rx 수")
    ax.set_xlabel("시퀀스 번호")
    ax.set_title("같은 프레임을 동시에 놓친 Rx 수  (높을수록 무선/송신 공통 문제)",
                 fontweight="bold")
    ax.set_ylim(0, n + 0.5); ax.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(outpath, dpi=110, bbox_inches="tight")
    plt.close()


# ──────────────────────────────────────────────
# 그림 3: CSI 진폭 히트맵 (Rx 1개)
# ──────────────────────────────────────────────
def plot_heatmap(data, label, outpath, max_frames=2000):
    amps = data[label]["amp"]
    amps = [a for a in amps if len(a) > 0]
    if not amps:
        print(f"[!] {label}: CSI 진폭 데이터 없음")
        return
    if len(amps) > max_frames:
        amps = amps[:max_frames]
    L = max(len(a) for a in amps)
    M = np.full((len(amps), L), np.nan, dtype=np.float32)
    for i, a in enumerate(amps):
        M[i, : len(a)] = a

    fig, ax = plt.subplots(figsize=(14, 5))
    im = ax.imshow(M.T, aspect="auto", origin="lower", cmap="viridis",
                   interpolation="nearest")
    fig.colorbar(im, ax=ax, label="진폭")
    ax.set_xlabel("프레임 인덱스")
    ax.set_ylabel("서브캐리어")
    ax.set_title(f"{label} CSI 진폭 히트맵  (세로 빈 줄 = 깨진/빠진 프레임)",
                 fontweight="bold")
    plt.tight_layout()
    plt.savefig(outpath, dpi=110, bbox_inches="tight")
    plt.close()


# ──────────────────────────────────────────────
def diagnose(stats, lost_per_seq, n, span):
    """동시유실 분포로 원인 자동 판정"""
    single = 100 * (lost_per_seq == 1).sum() / span
    multi = 100 * (lost_per_seq >= n - 1).sum() / span  # 거의 전부 동시
    any_lost = 100 * (lost_per_seq > 0).sum() / span

    print("\n" + "=" * 60)
    print("  자동 진단")
    print("=" * 60)
    print(f"  유실 발생 프레임 비율 : {any_lost:.1f}%")
    print(f"  단독 유실(1개 Rx)     : {single:.1f}%")
    print(f"  거의 전체 동시 유실   : {multi:.1f}%")
    print("-" * 60)
    if single > multi * 3 and single > 10:
        print("  → 유실이 대부분 '한 번에 한 Rx씩' 흩어져 발생.")
        print("     무선/송신 문제가 아니라 각 Rx의 USB·시리얼 로컬")
        print("     병목으로 추정됨. 다음을 점검:")
        print("       · 여러 USB 루트 허브로 분산 연결 (한 허브 집중 금지)")
        print("       · 수집 중 CPU 코어 포화 여부 (작업관리자)")
        print("       · 유실 높은 Rx의 케이블/포트 교체")
    elif multi > single:
        print("  → 여러 Rx가 같은 프레임을 동시에 놓침.")
        print("     송신측(AP) 또는 무선 채널 문제로 추정됨. 점검:")
        print("       · AP 송신 주기/채널 혼잡, 간섭원")
        print("       · Tx-Rx 거리·차폐")
    else:
        print("  → 단독·동시 유실이 혼재. USB 병목과 무선 영향이")
        print("     함께 작용하는 것으로 보임.")
    # Rx 편차
    rates = sorted(stats, key=lambda s: s["loss_rate"])
    print("-" * 60)
    print(f"  최저 유실 {rates[0]['label']} ({rates[0]['loss_rate']:.1f}%) ↔ "
          f"최고 유실 {rates[-1]['label']} ({rates[-1]['loss_rate']:.1f}%)")
    if rates[-1]["loss_rate"] > rates[0]["loss_rate"] * 2:
        print("    편차 큼 → 특정 Rx의 연결 경로(허브/케이블) 우선 점검.")
    print("=" * 60)


# ──────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="CSI CSV 사후 분석/시각화")
    ap.add_argument("--dir", default=".", help="CSV들이 있는 폴더")
    ap.add_argument("--glob", default="*rx*.csv", help="파일 패턴")
    ap.add_argument("--outdir", default=".", help="결과 이미지 저장 폴더")
    ap.add_argument("--heatmap", default=None,
                    help="진폭 히트맵 뽑을 Rx 라벨 (예: Rx7, all, none)")
    args = ap.parse_args()

    setup_korean_font()
    os.makedirs(args.outdir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(args.dir, args.glob)))
    if not files:
        print(f"[!] 파일 없음: {os.path.join(args.dir, args.glob)}")
        return
    print(f"[*] {len(files)}개 파일 로드 중...")
    data = load_dataset(files)
    if not data:
        print("[!] 유효 데이터 없음")
        return

    labels, mat, span, start, stats = compute_stats(data)

    # 통계 표
    print("\n" + "-" * 72)
    print(f"{'Rx':<6}{'수신':>8}{'유실':>8}{'유실%':>8}{'깨진행':>8}{'평균RSSI':>10}")
    print("-" * 72)
    for s in stats:
        print(f"{s['label']:<6}{s['received']:>8}{s['lost']:>8}"
              f"{s['loss_rate']:>7.1f}%{s['broken']:>8}{s['rssi_mean']:>9.1f}")
    print("-" * 72)

    p1 = os.path.join(args.outdir, "csi_summary.png")
    lost_per_seq = plot_summary(labels, mat, stats, p1)
    print(f"[저장] {p1}")

    p2 = os.path.join(args.outdir, "csi_loss_map.png")
    plot_loss_map(labels, mat, span, lost_per_seq, p2)
    print(f"[저장] {p2}")

    # 히트맵
    target = args.heatmap
    if target is None:
        # 기본: 유실 가장 적은 Rx 한 개
        target = min(stats, key=lambda s: s["loss_rate"])["label"]
    if target.lower() != "none":
        if target.lower() == "all":
            targets = labels
        else:
            # 대소문자 무시 매칭
            match = [l for l in labels if l.lower() == target.lower()]
            targets = match if match else [target]
        for t in targets:
            if t in data:
                p = os.path.join(args.outdir, f"csi_heatmap_{t}.png")
                plot_heatmap(data, t, p)
                print(f"[저장] {p}")
            else:
                print(f"[!] {t} 없음 (가능: {', '.join(labels)})")

    diagnose(stats, lost_per_seq, len(labels), span)


if __name__ == "__main__":
    main()