#!/usr/bin/env python3
# -*-coding:utf-8-*-

import serial
import csv
import sys
import os
import time
import multiprocessing as mp
import winsound  # 윈도우 내장 비프음 모듈

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
                        headers = strings.split(',')
                        headers.append("abs_timestamp")
                        csv_writer.writerow(strings.split(','))
                        header_written = True
                    continue

                if strings.startswith('CSI_DATA'):
                    row_data = strings.split(',')
                    
                    # 데이터 무결성 검증
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

                    # 메인 프로세스의 조기 컷 판단용: 현재까지 누적 카운트를 실시간 공유
                    # (매 프레임 dict 접근은 비싸므로 일정 주기로만 갱신)
                    if count % 20 == 0:
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
            # 화면에 바로 출력하지 않고 메인으로 보낼 딕셔너리에 저장
            results[rx_id] = f"[{rx_id}]-{port} 완료 - 총 {count} 프레임 | {rate:.1f} Hz | 에러 필터링: {error_count}"


def monitor_ui(configs, live_counts, stop_event, collect_start_time, duration, early_min_hz):
    """
    터미널 인플레이스 업데이트 방식 실시간 수신율 모니터 (별도 프로세스).
    pip 추가 설치 없이 동작. live_counts 읽기 전용이라 워커 수신율에 영향 없음.
    """
    MAX_HZ     = 35.0
    BAR_WIDTH  = 28
    UPDATE_SEC = 0.5
    RX_COUNT   = len(configs)

    # ANSI 이스케이프: 커서 N줄 위로 올리기
    def cursor_up(n):
        sys.stdout.write(f"\033[{n}A")

    def clamp_bar(hz):
        filled = int(min(hz / MAX_HZ, 1.0) * BAR_WIDTH)
        return '█' * filled + '░' * (BAR_WIDTH - filled)

    first_draw = True

    while not stop_event.is_set():
        now     = time.time()
        elapsed = max(now - collect_start_time.value, 0.001)
        remain  = max(duration - elapsed, 0)

        elapsed_str = time.strftime('%M:%S', time.gmtime(elapsed))
        remain_str  = time.strftime('%M:%S', time.gmtime(remain))
        total_str   = time.strftime('%M:%S', time.gmtime(duration))

        lines = []
        lines.append(
            f"┌─ CSI 실시간 수신 모니터  경과 {elapsed_str} / {total_str}  남은시간 {remain_str} ─┐"
        )
        lines.append(
            f"│  기준선 {early_min_hz} Hz │ 갱신 {UPDATE_SEC}s │ 전체 평균                              │"
        )
        lines.append("├" + "─" * 58 + "┤")

        low_list = []
        for conf in configs:
            rx_id = conf['rx_id']
            port  = conf['port']
            count = live_counts.get(rx_id, 0)
            hz    = count / elapsed
            bar   = clamp_bar(hz)
            ok    = hz >= early_min_hz
            mark  = '✓' if ok else '✗'
            warn  = ' ← 낮음!' if not ok else ''
            lines.append(
                f"│ {rx_id}({port:<5}) │{bar}│{hz:5.1f}Hz {mark}{warn:<8}│"
            )
            if not ok:
                low_list.append(f"{rx_id}({hz:.1f}Hz)")

        lines.append("├" + "─" * 58 + "┤")
        if low_list:
            status = f"⚠ 기준 미달: {', '.join(low_list)}"
        else:
            status = "✓ 전체 수신율 정상"
        lines.append(f"│ {status:<57}│")
        lines.append("└" + "─" * 58 + "┘")

        if not first_draw:
            cursor_up(len(lines))
        else:
            first_draw = False

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

        time.sleep(UPDATE_SEC)

    # 종료 메시지
    sys.stdout.write("\n[모니터 종료]\n")
    sys.stdout.flush()


if __name__ == '__main__':


    PERSON = 'test'       # 대상자 이름 (예: kjh ,jhj, swt, kmh / 두명은 kjh_swt, jhj_kmh 등)
    ACTION = 'test'      # 행동 (예: walk, sit, handsup)
    ZONE = '4'     # 구역 번호 (예: 1, 2, 3, 4 / 두명은 12, 23, 34, 13 등)


    PORTS = {
        'Rx1': 'COM7',
        'Rx2': 'COM10',
        'Rx3': 'COM11',
        'Rx4': 'COM9',
        'Rx5': 'COM3', # 실제 환경에 맞게 포트 번호 변경 필요
        'Rx6': 'COM4',
        'Rx7': 'COM5',
        'Rx8': 'COM6'

    }   

    save_dir = PERSON
    
    # 해당 이름의 폴더가 없으면 새로 만듭니다.
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
        print(f"[*] 새 폴더 생성됨: {save_dir}/")

    configs = []
    for rx_id, port in PORTS.items():
        filename = f"{PERSON}_{ACTION}_{ZONE}_{rx_id.lower()}.csv"

        filepath = os.path.join(save_dir, filename) 
        
        configs.append({'rx_id': rx_id, 'port': port, 'output': filepath})



    
    BAUDRATE = 2000000
    PREP_TIME = 6     # 시작 전 대기 시간 (초)
    DURATION = 120     # 수집 시간 (초)

    # ===== 조기 컷(early-cut) 설정 =====
    EARLY_CHECK_AT   = 20    # 시작 후 이 시점(초)에 수신율을 점검
    EARLY_WARMUP     = 3     # 초반 버퍼 동기화 출렁임 구간(초) - 이 구간은 측정에서 제외
    EARLY_MIN_HZ     = 23    # 점검 구간 평균이 이 값(Hz) 미만이면 재시작 권장
    # 33Hz 송신 기준, 점검 윈도우 = (EARLY_CHECK_AT - EARLY_WARMUP) = 17초.
    # 정상 Rx는 28~31Hz, 문제 Rx는 11~18Hz로 갈리는 이봉분포라 23Hz 컷이면
    # 둘 사이 빈 구간에 안전하게 들어감(오탐 최소화).

    # 모든 프로세스가 동시에 시작되도록 맞추는 동기화 이벤트 객체
    start_event = mp.Event()
    
    # 결과를 모을 공유 딕셔너리 생성 추가
    manager = mp.Manager()
    results = manager.dict()
    live_counts = manager.dict()   # 조기 컷 판단용 실시간 누적 카운트
    collect_start_time = manager.Value('d', 0.0)   # UI가 경과시간 계산에 쓸 시작 시각
    for rx_id in PORTS:
        live_counts[rx_id] = 0
    processes = []

    print(f"[*] 총 {len(configs)}개의 수신기를 초기화합니다...")
    
    # 2. 각 포트별로 독립적인 프로세스 생성 및 대기 상태 진입
    for conf in configs:
        p = mp.Process(target=collect_worker, args=(conf['rx_id'], conf['port'], conf['output'], BAUDRATE, DURATION, start_event, results, live_counts))
        p.start()
        processes.append(p)

    # 3. 측정 위치로 이동할 준비 시간 (5초)
    print(f"\n[*] {PREP_TIME}초 뒤 수집을 시작합니다. 측정 위치로 이동하세요!")
    for i in range(PREP_TIME, 0, -1):
        print(f" - {i}초 전...")
        time.sleep(1)

    # 4. 수집 시작
    print("\n[>> 수집 시작 <<]")
    winsound.Beep(1000, 800)  # 1000Hz 주파수로 0.8초간 삐- 소리 발생 (시작음, 그대로 유지)
    start_event.set()         # 대기 중이던 프로세스에 동시 수집 시작 신호 전송
    collect_start = time.time()         # 조기 컷 t=0 기준점
    collect_start_time.value = collect_start  # UI 프로세스와 공유

    # UI 모니터 프로세스 기동 (워커와 완전히 분리, live_counts 읽기 전용)
    ui_stop = mp.Event()
    ui_proc = mp.Process(
        target=monitor_ui,
        args=(configs, live_counts, ui_stop, collect_start_time, DURATION, EARLY_MIN_HZ),
        daemon=True
    )
    ui_proc.start()

    # 5. 조기 컷 점검: warmup 구간 카운트를 기준선으로 잡고, 점검 시점까지 대기
    time.sleep(EARLY_WARMUP)
    warmup_counts = {rx_id: live_counts.get(rx_id, 0) for rx_id in PORTS}

    # 점검 시점까지 남은 시간 대기 (이미 흐른 시간 보정)
    remain = EARLY_CHECK_AT - (time.time() - collect_start)
    if remain > 0:
        time.sleep(remain)

    # 점검 윈도우(EARLY_WARMUP ~ EARLY_CHECK_AT) 동안의 순간 수신율 계산
    window = EARLY_CHECK_AT - EARLY_WARMUP
    low_rx = []   # (rx_id, port, hz) 미달 목록
    print(f"\n[*] 조기 점검 ({EARLY_CHECK_AT}초 시점, 최근 {window}초 기준 / 기준 {EARLY_MIN_HZ}Hz)")
    for conf in configs:
        rx_id, port = conf['rx_id'], conf['port']
        delta = live_counts.get(rx_id, 0) - warmup_counts.get(rx_id, 0)
        hz = delta / window if window > 0 else 0
        mark = "OK " if hz >= EARLY_MIN_HZ else "LOW"
        print(f"    [{mark}] {rx_id}-{port}: {hz:.1f} Hz")
        if hz < EARLY_MIN_HZ:
            low_rx.append((rx_id, port, hz))

    if low_rx:
        # 미달 Rx 존재 -> 조기 종료 + 삐-삐- + 재시작 권장 메시지
        ui_stop.set()
        ui_proc.terminate()

        winsound.Beep(1000, 300)
        time.sleep(0.1)
        winsound.Beep(1000, 300)

        for p in processes:
            p.terminate()
        for p in processes:
            p.join()

        print("\n[!! 조기 종료 - 재시작 권장 !!]")
        names = ", ".join(f"{rx_id}({port}, {hz:.1f}Hz)" for rx_id, port, hz in low_rx)
        print(f"[!] {names} 의 수신율이 낮아 재시작이 좋아보입니다.")
        sys.exit(1)

    # 점검 통과 -> 남은 시간 그대로 진행
    print("[*] 조기 점검 통과. 수집을 계속합니다.\n")
    for p in processes:
        p.join()

    # UI 모니터 종료 (수집 끝났으므로 stop 신호 전송)
    ui_stop.set()
    ui_proc.join(timeout=5)

    # 6. 수집 종료
    print("\n[<< 수집 종료 >>]")
    winsound.Beep(1000, 300)  # 삐-
    time.sleep(0.1)
    winsound.Beep(1000, 300)  # 삐-
    
    # Rx1부터 Rx4까지 순서대로 출력
    print("\n[수집 요약]")
    for i in range(1, len(PORTS) + 1):
        key = f"Rx{i}"
        if key in results:
            print(results[key])
    
    print("\n[*] 모든 CSV 파일 저장이 완료되었습니다.")