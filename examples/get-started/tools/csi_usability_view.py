#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
CSI 사용성 판단 시각화
─────────────────────────────────────────────
"수신율이 낮게 수집된 데이터를 학습/분석에 그대로 써도 되는가?" 를
사람이 눈으로 판단하기 위한 도구.

수신율(Hz) 수치 하나만으로는 폐기 여부를 단정할 수 없다. 핵심은:
  · 유실이 '균일한 다운샘플링' 인가  → 프레임 간격이 고르면 신호 구조 보존 → 사용 가능
  · 유실이 '구간 블랙아웃(burst)' 인가 → 특정 시간이 통째로 비면 행동 정보 손실 → 사용 곤란

그래서 아래 5가지를 함께 본다:
  1) CSI 진폭 히트맵 (서브캐리어 × 프레임)  : 신호 구조/행동 패턴이 보이는가
  2) 시간축 수신율 (슬라이딩 Hz)            : 어느 구간에서 떨어졌는가
  3) 프레임 간격 분포                       : 균일 다운샘플 vs 버스트 블랙아웃
  4) 대표 서브캐리어 진폭 시계열            : 행동 신호가 살아있는가
  5) RSSI 시계열                            : 채널 차폐 정도

그리고 위 지표로 '사용 가능 / 주의 / 사용 곤란' 자동 판정을 출력한다.

────────────────────────────────────────────────────────────────
[ 사용법 ]  아래 ■ 설정 블록의 변수만 바꾸고 그냥 실행하면 된다.
            파일명 만드는 방식은 수집 스크립트(multi_rx_collect_csi_v3.py)와 동일:
                {PERSON}/{PERSON}_{ACTION}_{ZONE}_{rx}.csv
────────────────────────────────────────────────────────────────
"""

import os
import csv
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams


# ══════════════════════════════════════════════════════════════
# ■ 설정 블록 — 여기만 바꾸면 됩니다
# ══════════════════════════════════════════════════════════════
PERSON = 'jhj_kec'        # 대상자 이니셜 (= 폴더명). 2인은 'kjh_swt' 처럼 결합
ACTION = 'sit'        # 행동 (예: walk, sit, stand, handsup)
ZONE   = '1_2'          # 구역 번호 (1 ~ 4)

# 어떤 Rx를 볼지 선택:
#   'rx1' ~ 'rx8'  → 해당 Rx 1개만
#   'all'          → 존재하는 모든 Rx (rx1~rx8)를 각각 + 비교 그래프까지
RX     = 'all'

# 결과 PNG 저장 폴더 (기본: 데이터 폴더 안의 _view 하위폴더)
OUTDIR = None         # None 이면 자동으로 "{PERSON}/_view" 사용

# 목표 수신율(Hz). 균일 다운샘플 판정 기준에 사용
TARGET_HZ = 30.0

# 진폭 시계열로 그릴 대표 서브캐리어 개수 (균등 간격으로 자동 선택)
N_SUBCARRIER_LINES = 4

# 히트맵에 그릴 최대 프레임 수 (너무 길면 잘라서 그림)
MAX_FRAMES_HEATMAP = 4000
# ══════════════════════════════════════════════════════════════


# CSV 컬럼 위치 (헤더 없는 raw CSI_DATA 포맷)
SEQ_COL   = 2
RSSI_COL  = 4
CSI_START = 25     # CSI I/Q 시작 컬럼
# col[-1] = 수집기가 붙인 절대 수신시각(epoch)


# ── 한글 폰트 ─────────────────────────────────
def setup_korean_font():
    for name in ("Malgun Gothic", "AppleGothic", "NanumGothic", "NanumBarunGothic"):
        if any(name in f.name for f in font_manager.fontManager.ttflist):
            rcParams["font.family"] = name
            break
    rcParams["axes.unicode_minus"] = False


# ── 파일명 생성 (수집 스크립트와 동일 규칙) ──────
def build_path(person, action, zone, rx):
    filename = f"{person}_{action}_{zone}_{rx.lower()}.csv"
    return os.path.join(person, filename)


# ──────────────────────────────────────────────
# 파일 한 개 파싱
#   반환: seq, rssi, time(epoch), amp(list of np.array), broken수
# ──────────────────────────────────────────────
def load_file(path):
    seqs, rssis, times, amps = [], [], [], []
    broken = 0
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0] != "CSI_DATA" or len(row) < 28:
                broken += 1
                continue
            # 한 줄에 두 프레임이 붙은 깨진 행
            if len(row) > 8 and row[8] == "CSI_DATA":
                broken += 1
                continue
            try:
                seq  = int(row[SEQ_COL])
                rssi = int(row[RSSI_COL])
            except (ValueError, IndexError):
                broken += 1
                continue
            if rssi > 0 or rssi < -100:
                broken += 1
                continue

            # 마지막 컬럼 = 절대 수신시각(epoch)
            try:
                t = float(row[-1])
            except (ValueError, IndexError):
                t = np.nan

            # CSI raw I/Q: col[25] ~ 끝-1
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
            times.append(t)
            amps.append(amp)

    return (np.array(seqs), np.array(rssis, dtype=np.float64),
            np.array(times, dtype=np.float64), amps, broken)


# ──────────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────────
def compute_metrics(seqs, times):
    """수신율/유실/간격 관련 지표 딕셔너리 반환"""
    n = len(seqs)
    out = {"received": n}

    # seq 기반 유실률 (송신은 빠짐없이 1씩 증가한다고 가정)
    expected = int(seqs[-1] - seqs[0] + 1) if n > 1 else n
    out["expected"] = expected
    out["lost"] = expected - n
    out["loss_rate"] = 100.0 * (expected - n) / expected if expected > 0 else 0.0

    # 실제 경과시간 기반 평균 Hz
    valid_t = times[np.isfinite(times)]
    if len(valid_t) > 1:
        dur = valid_t[-1] - valid_t[0]
        out["duration"] = dur
        out["hz_mean"] = (len(valid_t) - 1) / dur if dur > 0 else 0.0
        gaps = np.diff(valid_t)
        gaps = gaps[gaps > 0]
    else:
        out["duration"] = 0.0
        out["hz_mean"] = 0.0
        gaps = np.array([])

    out["gaps"] = gaps
    if len(gaps) > 0:
        nominal = 1.0 / TARGET_HZ
        out["gap_median"] = float(np.median(gaps))
        out["gap_p95"] = float(np.percentile(gaps, 95))
        out["gap_max"] = float(np.max(gaps))
        # '블랙아웃' = 정상 간격의 5배 이상으로 벌어진 구멍
        blackout = gaps[gaps > nominal * 5]
        out["blackout_count"] = int(len(blackout))
        out["blackout_total"] = float(blackout.sum())
        out["blackout_ratio"] = 100.0 * blackout.sum() / (valid_t[-1] - valid_t[0]) \
            if len(valid_t) > 1 and (valid_t[-1] - valid_t[0]) > 0 else 0.0
        # 간격 균일도: 변동계수(CV)= std/mean (작을수록 균일 다운샘플)
        out["gap_cv"] = float(np.std(gaps) / np.mean(gaps)) if np.mean(gaps) > 0 else 0.0
    else:
        out.update(gap_median=0, gap_p95=0, gap_max=0,
                   blackout_count=0, blackout_total=0, blackout_ratio=0, gap_cv=0)
    return out


def verdict(m):
    """지표 기반 자동 사용성 판정. (등급, 색, 사유리스트) 반환"""
    reasons = []
    hz = m["hz_mean"]
    cv = m["gap_cv"]
    blackout_ratio = m["blackout_ratio"]
    gap_max = m["gap_max"]

    # 블랙아웃(구간 통째로 비는 것)이 가장 치명적
    bad = False
    warn = False

    if blackout_ratio > 8 or gap_max > 2.0:
        bad = True
        reasons.append(f"긴 블랙아웃 존재 (시간의 {blackout_ratio:.1f}%, 최대 공백 {gap_max:.2f}s)"
                       " → 해당 구간 행동 정보 손실")
    elif blackout_ratio > 3 or gap_max > 1.0:
        warn = True
        reasons.append(f"중간 길이 공백 일부 존재 (시간의 {blackout_ratio:.1f}%, 최대 {gap_max:.2f}s)")

    if hz < TARGET_HZ * 0.4:   # 12Hz 미만(30Hz 기준)
        if cv < 0.6:
            warn = True
            reasons.append(f"수신율 낮음({hz:.1f}Hz)이나 간격은 비교적 균일(CV={cv:.2f})"
                           " → 균일 다운샘플 성격")
        else:
            bad = True
            reasons.append(f"수신율 낮고({hz:.1f}Hz) 간격도 불균일(CV={cv:.2f}) → 유실이 들쭉날쭉")
    elif cv > 0.9:
        warn = True
        reasons.append(f"간격 불균일(CV={cv:.2f}) → 유실이 고르지 않음")

    if bad:
        return "사용 곤란", "#d62728", reasons
    if warn:
        return "주의 (검토 후 사용)", "#ff7f0e", reasons or ["일부 지표가 경계선"]
    return "사용 가능", "#2ca02c", reasons or [f"수신율 {hz:.1f}Hz, 간격 균일(CV={cv:.2f}), 큰 공백 없음"]


# ──────────────────────────────────────────────
# 단일 Rx 종합 그림
# ──────────────────────────────────────────────
def plot_single(label, seqs, rssis, times, amps, m, grade, gcolor, reasons, outpath):
    amps_nz = [a for a in amps if len(a) > 0]
    L = max((len(a) for a in amps_nz), default=0)

    fig = plt.figure(figsize=(15, 12))
    gs = fig.add_gridspec(4, 2, height_ratios=[2.2, 1, 1, 1], hspace=0.5, wspace=0.2)

    # 상대시간(초)
    t0 = times[np.isfinite(times)][0] if np.isfinite(times).any() else 0
    rel_t = times - t0

    # ── (1) CSI 진폭 히트맵 ─────────────────────
    ax = fig.add_subplot(gs[0, :])
    if amps_nz and L > 0:
        use = amps[:MAX_FRAMES_HEATMAP]
        M = np.full((len(use), L), np.nan, dtype=np.float32)
        for i, a in enumerate(use):
            if len(a) > 0:
                M[i, :len(a)] = a
        im = ax.imshow(M.T, aspect="auto", origin="lower", cmap="viridis",
                       interpolation="nearest")
        fig.colorbar(im, ax=ax, label="진폭", pad=0.01)
        ax.set_xlabel("프레임 인덱스  (세로 빈 줄 = 깨진/빠진 프레임)")
        ax.set_ylabel("서브캐리어")
    else:
        ax.text(0.5, 0.5, "CSI 진폭 데이터 없음", ha="center", va="center")
    ax.set_title(f"[{label}]  CSI 진폭 히트맵 — 행동 패턴/신호 구조 확인", fontweight="bold")

    # ── (2) 시간축 수신율 (슬라이딩 Hz) ──────────
    ax = fig.add_subplot(gs[1, :])
    valid = np.isfinite(times)
    vt = rel_t[valid]
    if len(vt) > 2:
        # 1초 윈도우로 초당 프레임 수 계산
        bins = np.arange(0, np.ceil(vt[-1]) + 1, 1.0)
        hzc, edges = np.histogram(vt, bins=bins)
        centers = (edges[:-1] + edges[1:]) / 2
        ax.plot(centers, hzc, lw=1.2, color="#1f77b4")
        ax.axhline(TARGET_HZ, color="gray", ls="--", lw=0.8, label=f"목표 {TARGET_HZ:.0f}Hz")
        ax.axhline(m["hz_mean"], color="green", ls=":", lw=1.0,
                   label=f"평균 {m['hz_mean']:.1f}Hz")
        ax.fill_between(centers, 0, hzc, where=(hzc < TARGET_HZ * 0.4),
                        color="red", alpha=0.2, step="mid")
        ax.legend(fontsize=8, loc="upper right")
    ax.set_ylabel("초당 프레임 (Hz)")
    ax.set_xlabel("시간 (초)")
    ax.set_title("시간축 수신율 — 빨강 음영 = 수신율 급락 구간", fontweight="bold")
    ax.grid(alpha=0.3)

    # ── (3) 프레임 간격 분포 ────────────────────
    ax = fig.add_subplot(gs[2, 0])
    gaps = m["gaps"]
    if len(gaps) > 0:
        gms = gaps * 1000.0  # ms
        ax.hist(gms, bins=60, range=(0, min(gms.max(), 500)),
                color="#9467bd", alpha=0.8)
        ax.axvline(1000.0 / TARGET_HZ, color="green", ls="--", lw=1,
                   label=f"정상 간격 {1000.0/TARGET_HZ:.0f}ms")
        ax.legend(fontsize=8)
    ax.set_xlabel("프레임 간격 (ms)")
    ax.set_ylabel("빈도")
    ax.set_title(f"간격 분포 (CV={m['gap_cv']:.2f}, 낮을수록 균일)", fontweight="bold")
    ax.grid(alpha=0.3)

    # ── (3b) 간격 시계열 (블랙아웃 위치) ─────────
    ax = fig.add_subplot(gs[2, 1])
    if len(vt) > 1:
        ax.plot(vt[1:], np.diff(vt) * 1000.0, lw=0.6, color="#8c564b")
        ax.axhline(5000.0 / TARGET_HZ, color="red", ls="--", lw=0.8,
                   label="블랙아웃 기준(5×)")
        ax.legend(fontsize=8)
    ax.set_xlabel("시간 (초)")
    ax.set_ylabel("직전 간격 (ms)")
    ax.set_title("간격 시계열 — 위로 튄 지점 = 공백", fontweight="bold")
    ax.grid(alpha=0.3)

    # ── (4) 대표 서브캐리어 진폭 시계열 ──────────
    ax = fig.add_subplot(gs[3, 0])
    if amps_nz and L > 0:
        idxs = np.linspace(L * 0.15, L * 0.85, N_SUBCARRIER_LINES).astype(int)
        idxs = sorted(set(int(i) for i in idxs if 0 <= i < L))
        series = {i: [] for i in idxs}
        xs = []
        for fi, a in enumerate(amps):
            if len(a) > max(idxs):
                xs.append(rel_t[fi] if np.isfinite(rel_t[fi]) else fi)
                for i in idxs:
                    series[i].append(a[i])
        for i in idxs:
            ax.plot(xs, series[i], lw=0.7, label=f"sc{i}")
        ax.legend(fontsize=7, ncol=2)
    ax.set_xlabel("시간 (초)")
    ax.set_ylabel("진폭")
    ax.set_title("대표 서브캐리어 진폭 — 행동 신호가 살아있는가", fontweight="bold")
    ax.grid(alpha=0.3)

    # ── (5) RSSI 시계열 ─────────────────────────
    ax = fig.add_subplot(gs[3, 1])
    if len(vt) == len(rssis[valid]):
        ax.plot(vt, rssis[valid], lw=0.7, color="#e377c2")
    ax.set_xlabel("시간 (초)")
    ax.set_ylabel("RSSI (dBm)")
    ax.set_title(f"RSSI 시계열 (평균 {np.mean(rssis):.1f} dBm)", fontweight="bold")
    ax.grid(alpha=0.3)

    # ── 상단 종합 판정 타이틀 ───────────────────
    head = (f"{label}   |   수신 {m['received']}프레임 / 유실 {m['loss_rate']:.1f}% / "
            f"평균 {m['hz_mean']:.1f}Hz   →   판정: 【 {grade} 】")
    fig.suptitle(head, fontsize=14, fontweight="bold", color=gcolor, y=0.995)
    sub = "  ·  ".join(reasons)
    fig.text(0.5, 0.965, sub, ha="center", fontsize=9, color="#444444")

    plt.savefig(outpath, dpi=110, bbox_inches="tight")
    plt.close()


# ──────────────────────────────────────────────
# 여러 Rx 비교 (RX='all' 일 때)
# ──────────────────────────────────────────────
def plot_compare(per_rx, outpath):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9), height_ratios=[1, 1.2])

    # 위: Rx별 평균 Hz 막대 + 판정 색
    labels = list(per_rx.keys())
    hzs = [per_rx[k]["m"]["hz_mean"] for k in labels]
    colors = [per_rx[k]["gcolor"] for k in labels]
    bars = ax1.bar(labels, hzs, color=colors)
    for b, h in zip(bars, hzs):
        ax1.text(b.get_x() + b.get_width() / 2, h + 0.3, f"{h:.1f}",
                 ha="center", va="bottom", fontsize=9)
    ax1.axhline(TARGET_HZ, color="gray", ls="--", lw=0.8, label=f"목표 {TARGET_HZ:.0f}Hz")
    ax1.set_ylabel("평균 수신율 (Hz)")
    ax1.set_title("Rx별 평균 수신율 (막대색 = 사용성 판정)", fontweight="bold")
    ax1.legend(fontsize=8)
    ax1.grid(axis="y", alpha=0.3)

    # 아래: Rx별 시간축 수신율 겹쳐 그리기
    for k in labels:
        vt = per_rx[k]["rel_t"]
        vt = vt[np.isfinite(vt)]
        if len(vt) > 2:
            bins = np.arange(0, np.ceil(vt[-1]) + 1, 1.0)
            hzc, edges = np.histogram(vt, bins=bins)
            centers = (edges[:-1] + edges[1:]) / 2
            ax2.plot(centers, hzc, lw=1.0, label=k)
    ax2.axhline(TARGET_HZ, color="gray", ls="--", lw=0.8)
    ax2.set_xlabel("시간 (초)")
    ax2.set_ylabel("초당 프레임 (Hz)")
    ax2.set_title("Rx별 시간축 수신율 — 동시에 떨어지면 무선/송신 공통 문제", fontweight="bold")
    ax2.legend(fontsize=8, ncol=len(labels))
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(outpath, dpi=110, bbox_inches="tight")
    plt.close()


# ──────────────────────────────────────────────
def process_one(person, action, zone, rx, outdir):
    path = build_path(person, action, zone, rx)
    if not os.path.exists(path):
        print(f"[!] 파일 없음: {path}")
        return None

    seqs, rssis, times, amps, broken = load_file(path)
    if len(seqs) < 2:
        print(f"[!] {path}: 유효 프레임 부족 (broken={broken})")
        return None

    label = f"{person}_{action}_{zone}_{rx.lower()}"
    m = compute_metrics(seqs, times)
    grade, gcolor, reasons = verdict(m)

    # 콘솔 요약
    print("\n" + "─" * 70)
    print(f"  {label}")
    print("─" * 70)
    print(f"  수신 프레임   : {m['received']}   (깨진 행 {broken})")
    print(f"  seq 유실률    : {m['loss_rate']:.1f}%   (기대 {m['expected']}, 유실 {m['lost']})")
    print(f"  평균 수신율   : {m['hz_mean']:.1f} Hz   (측정 {m['duration']:.1f}s)")
    print(f"  간격 중앙값   : {m['gap_median']*1000:.1f} ms | p95 {m['gap_p95']*1000:.1f} ms "
          f"| 최대 {m['gap_max']*1000:.0f} ms")
    print(f"  간격 균일도CV : {m['gap_cv']:.2f}   (낮을수록 균일 다운샘플)")
    print(f"  블랙아웃      : {m['blackout_count']}회 / 총 {m['blackout_total']:.2f}s "
          f"({m['blackout_ratio']:.1f}% of time)")
    print(f"  ▶ 판정        : 【 {grade} 】")
    for r in reasons:
        print(f"      - {r}")

    outpath = os.path.join(outdir, f"{label}_usability.png")
    plot_single(label, seqs, rssis, times, amps, m, grade, gcolor, reasons, outpath)
    print(f"  [저장] {outpath}")

    t0 = times[np.isfinite(times)][0] if np.isfinite(times).any() else 0
    return {"m": m, "gcolor": gcolor, "grade": grade, "rel_t": times - t0}


def main():
    setup_korean_font()

    outdir = OUTDIR if OUTDIR else os.path.join(PERSON, "_view")
    os.makedirs(outdir, exist_ok=True)

    print("=" * 70)
    print(f"  CSI 사용성 판단 시각화")
    print(f"  대상: PERSON={PERSON}  ACTION={ACTION}  ZONE={ZONE}  RX={RX}")
    print(f"  목표 수신율: {TARGET_HZ:.0f}Hz   결과폴더: {outdir}")
    print("=" * 70)

    if RX.lower() == "all":
        rx_list = [f"rx{i}" for i in range(1, 9)]
    else:
        rx_list = [RX]

    per_rx = {}
    for rx in rx_list:
        res = process_one(PERSON, ACTION, ZONE, rx, outdir)
        if res:
            per_rx[rx.lower()] = res

    if not per_rx:
        print("\n[!] 시각화할 유효 데이터가 없습니다. 설정(PERSON/ACTION/ZONE/RX)을 확인하세요.")
        return

    if len(per_rx) > 1:
        cmp_path = os.path.join(outdir, f"{PERSON}_{ACTION}_{ZONE}_compare.png")
        plot_compare(per_rx, cmp_path)
        print(f"\n[저장] {cmp_path}  (Rx 비교)")

    print("\n[*] 완료.")


if __name__ == "__main__":
    main()
