#!/usr/bin/env python3
# -*-coding:utf-8-*-

import serial
import csv
import sys
import os
import time
import multiprocessing as mp
import winsound  # 윈도우 내장 비프음 모듈
from collections import deque

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, TimeElapsedColumn
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

console = Console()

# ──────────────────────────────────────────────
# 워커: 변경 없음
# ──────────────────────────────────────────────
def collect_worker(rx_id, port, output_file, baudrate, duration, start_event, results, live_counts):
    """단일 포트에서 CSI 데이터를 수집하는 워커 프로세스"""
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=0.1
        )
        try:
            ser.set_buffer_size(rx_size=65536, tx_size=65536)
        except:
            pass

        print(f"[*] {port} 연결 완료. 대기 중...")

        # 메인 프로세스에서 시작 신호(Event)를 줄 때까지 대기
        start_event.wait()

        # 시작 신호가 떨어지면 버퍼에 쌓여있던 옛날 데이터를 날리고 정확히 동기화된 시점부터 수집 시작
        ser.reset_input_buffer()

    except serial.SerialException as e:
        print(f"[!] {port} 연결 실패: {e}")
        return

    # 파일 쓰기
    with open(output_file, 'w', newline='', buffering=65536) as csvfile:
        csv_writer = csv.writer(csvfile)
        header_written = False

        start_time = time.time()
        count = 0
        error_count = 0
        last_timestamp = 0

        try:
            while True:
                if duration is not None and (time.time() - start_time) >= duration:
                    break

                line_bytes = ser.readline()
                current_abs_time = time.time()
                if not line_bytes:
                    continue

                try:
                    strings = line_bytes.decode('utf-8', errors='ignore').strip()
                except Exception:
                    error_count += 1
                    continue

                if not strings:
                    continue

                if "type,recv_mac,seq" in strings or "type,recv_mac,id" in strings:
                    if not header_written:
                        csv_writer.writerow(strings.split(','))
                        header_written = True
                    continue

                if strings.startswith('CSI_DATA'):
                    row_data = strings.split(',')

                    if len(row_data) < 25:
                        error_count += 1
                        continue

                    try:
                        timestamp = int(row_data[19])
                        last_timestamp = timestamp
                    except (ValueError, IndexError):
                        error_count += 1
                        continue

                    try:
                        rssi = int(row_data[4])
                        if rssi > 0 or rssi < -100:
                            error_count += 1
                            continue
                    except (ValueError, IndexError):
                        error_count += 1
                        continue

                    row_data.append(f"{current_abs_time:.6f}")
                    csv_writer.writerow(row_data)
                    count += 1

                    # UI 1초당 수신율 계산용 실시간 공유 (5프레임마다 갱신)
                    # v4: 1초 윈도우 순간 Hz를 매끄럽게 계산하려고 갱신 주기를 촘촘히 함
                    if count % 5 == 0:
                        live_counts[rx_id] = count

                    if count % 200 == 0:
                        csvfile.flush()

        except KeyboardInterrupt:
            pass
        finally:
            csvfile.flush()
            if ser.is_open:
                ser.close()

            live_counts[rx_id] = count

            elapsed = time.time() - start_time
            rate = count / elapsed if elapsed > 0 else 0
            results[rx_id] = f"[{rx_id}]-{port} 완료 - 총 {count} 프레임 | {rate:.1f} Hz | 에러 필터링: {error_count}"


# ──────────────────────────────────────────────
# Rich UI 렌더러: 매 갱신마다 호출, Renderable 반환
#   v4 변경점:
#     - 수신율과 무관하게 막대/Hz/상태를 항상 초록으로 표시(낮아도 빨강 안 됨)
#     - 표시 Hz를 '전체 측정시간 평균'이 아니라 '최근 1초 순간 수신율'로 계산
#       (rx별 (시각, 누적카운트) 이력을 hz_hist 슬라이딩 윈도우로 들고 차분)
# ──────────────────────────────────────────────
def build_ui(configs, live_counts, collect_start, duration, hz_hist, recent_window, note=""):
    MAX_HZ    = 35.0
    BAR_CHARS = 24   # 막대 문자 수

    now      = time.time()
    elapsed  = max(now - collect_start, 0.001)
    remain   = max(duration - elapsed, 0)
    pct      = min(elapsed / duration, 1.0)

    elapsed_str = time.strftime('%M:%S', time.gmtime(elapsed))
    remain_str  = time.strftime('%M:%S', time.gmtime(remain))
    total_str   = time.strftime('%M:%S', time.gmtime(duration))

    # 진행 바 (Rich markup으로 직접 그림)
    bar_filled = int(pct * 40)
    bar_str    = "█" * bar_filled + "░" * (40 - bar_filled)
    prog_line  = Text()
    prog_line.append(f" 진행  ", style="bold cyan")
    prog_line.append(bar_str, style="cyan")
    prog_line.append(f"  {elapsed_str} / {total_str}  (남은 시간 {remain_str})", style="white")

    # Rx별 수신율 테이블
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on grey23",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("수신기",    style="bold", width=12, justify="center")
    table.add_column("포트",      width=7,  justify="center")
    table.add_column("수신율",    width=30, justify="left")
    table.add_column("Hz",        width=8,  justify="right")
    table.add_column("상태",      width=6,  justify="center")

    for conf in configs:
        rx_id = conf['rx_id']
        port  = conf['port']
        count = live_counts.get(rx_id, 0)

        # ── 최근 1초 순간 수신율(Hz) ──────────────
        # 이력에 (현재시각, 누적카운트)를 넣고, 윈도우보다 오래된 표본은 버린다.
        # 윈도우 시작 직전 표본 한 개는 남겨 전체 구간 차분을 잡는다.
        hist = hz_hist[rx_id]
        hist.append((now, count))
        while len(hist) >= 2 and (now - hist[1][0]) >= recent_window:
            hist.popleft()
        t0, c0 = hist[0]
        dt = now - t0
        if dt >= 0.3:
            hz = (count - c0) / dt          # 최근 구간 순간 수신율
        else:
            hz = count / elapsed            # 시작 직후 표본 부족 시 누적평균으로 대체

        # 막대 (수신율 무관 항상 초록)
        filled   = int(min(hz / MAX_HZ, 1.0) * BAR_CHARS)
        bar_text = Text()
        bar_text.append("█" * filled,             style="green")
        bar_text.append("░" * (BAR_CHARS - filled), style="grey50")

        hz_text     = Text(f"{hz:.1f}", style="green")
        status_text = Text("✓ OK", style="green")

        table.add_row(rx_id, port, bar_text, hz_text, status_text)

    # 하단 상태 메시지 (수신율 미달 경고 없음 — 항상 진행 표시)
    if note:
        footer = Text(f" {note}", style="bold yellow")
    else:
        footer = Text(" ✓  수집 진행 중  (수신율이 낮아도 계속 기록합니다)", style="bold green")

    # Panel 안에 진행바 + 테이블 함께 넣기
    from rich.console import Group
    return Group(
        Panel(
            Group(prog_line, table),
            title="[bold cyan]CSI 실시간 수신 모니터[/bold cyan]",
            subtitle=footer.__str__(),
            border_style="cyan",
        )
    )


# ──────────────────────────────────────────────
# 메인 모을게
# ──────────────────────────────────────────────
if __name__ == '__main__':

    PERSON = 'mhe_gyj'       # 대상자 이름 (예: kjh, jhj, swt, kmh / 두명은 kjh_swt 등)
    ACTION = 'handsup'    # 행동 (예: walk, sit, handsup)
    ZONE   = '2_3'          # 구역 번호 (예: 1, 2, 3, 4 / 1_2, 1_3, 1_4, 2_3, 2_4, 3_4 )

    PORTS = {
        'Rx1': 'COM7',
        'Rx2': 'COM10',
        'Rx3': 'COM11',
        'Rx4': 'COM9',
        'Rx5': 'COM3',
        'Rx6': 'COM4',
        'Rx7': 'COM5',
        'Rx8': 'COM6',
    }

    save_dir = PERSON
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
        console.print(f"[*] 새 폴더 생성됨: {save_dir}/", style="yellow")

    configs = []
    for rx_id, port in PORTS.items():
        filename = f"{PERSON}_{ACTION}_{ZONE}_{rx_id.lower()}.csv"
        filepath = os.path.join(save_dir, filename)
        configs.append({'rx_id': rx_id, 'port': port, 'output': filepath})

    BAUDRATE       = 2000000
    PREP_TIME      = 10
    DURATION       = 120    # 1명 기준 180초, 2명 기준 120초, 부재 기준 3240

    UPDATE_SEC       = 0.5  # UI 갱신 주기
    RECENT_HZ_WINDOW = 1.0  # 표시 수신율을 '최근 N초' 기준으로 계산 (1초당 수신율)

    start_event = mp.Event()
    manager     = mp.Manager()
    results     = manager.dict()
    live_counts = manager.dict()
    for rx_id in PORTS:
        live_counts[rx_id] = 0
    # rx별 최근 (시각, 누적카운트) 이력 — 1초당 수신율 계산용
    hz_hist = {rx_id: deque() for rx_id in PORTS}
    processes = []

    console.print(f"\n[bold][*] 총 {len(configs)}개의 수신기를 초기화합니다...[/bold]")

    for conf in configs:
        p = mp.Process(
            target=collect_worker,
            args=(conf['rx_id'], conf['port'], conf['output'],
                  BAUDRATE, DURATION, start_event, results, live_counts)
        )
        p.start()
        processes.append(p)

    console.print(f"\n[bold cyan][*] {PREP_TIME}초 뒤 수집을 시작합니다. 측정 위치로 이동하세요![/bold cyan]")
    for i in range(PREP_TIME, 0, -1):
        console.print(f" - {i}초 전...")
        time.sleep(1)

    console.print("\n[bold green][>> 수집 시작 <<][/bold green]")
    winsound.Beep(1000, 800)
    start_event.set()
    collect_start = time.time()

    # ── Rich Live UI + 수집 완료 대기 ──
    #    v4 변경점: 조기 컷(수신율 미달 시 중단) 로직 전부 제거.
    #             수신율이 낮아도 DURATION 동안 끝까지 기록을 이어간다.
    with Live(
        build_ui(configs, live_counts, collect_start, DURATION, hz_hist, RECENT_HZ_WINDOW),
        console=console,
        refresh_per_second=1 / UPDATE_SEC,
        transient=False,
    ) as live:

        while True:
            # UI 갱신
            live.update(build_ui(configs, live_counts, collect_start, DURATION,
                                 hz_hist, RECENT_HZ_WINDOW))

            # 모든 워커 종료 확인
            if all(not p.is_alive() for p in processes):
                break

            time.sleep(UPDATE_SEC)

    # ── 수집 종료 ──────────────────────────────
    console.print("\n[bold green][<< 수집 종료 >>][/bold green]")
    winsound.Beep(1000, 300)
    time.sleep(0.1)
    winsound.Beep(1000, 300)

    console.print("\n[bold][수집 요약][/bold]")
    for i in range(1, len(PORTS) + 1):
        key = f"Rx{i}"
        if key in results:
            console.print(results[key])

    console.print("\n[bold green][*] 모든 CSV 파일 저장이 완료되었습니다.[/bold green]")
