#!/usr/bin/env python3
# -*-coding:utf-8-*-

import serial
import csv
import sys
import os
import time
import multiprocessing as mp
import winsound  # 윈도우 내장 비프음 모듈

def collect_worker(rx_id, port, output_file, baudrate, duration, start_event, results):
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

                    if count % 200 == 0:
                        csvfile.flush()

        except KeyboardInterrupt:
            pass
        finally:
            csvfile.flush()
            if ser.is_open:
                ser.close()

            elapsed = time.time() - start_time
            rate = count / elapsed if elapsed > 0 else 0
            # 화면에 바로 출력하지 않고 메인으로 보낼 딕셔너리에 저장
            results[rx_id] = f"[{rx_id}]-{port} 완료 - 총 {count} 프레임 | {rate:.1f} Hz | 에러 필터링: {error_count}"


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

    # 모든 프로세스가 동시에 시작되도록 맞추는 동기화 이벤트 객체
    start_event = mp.Event()
    
    # 결과를 모을 공유 딕셔너리 생성 추가
    manager = mp.Manager()
    results = manager.dict()
    processes = []

    print(f"[*] 총 {len(configs)}개의 수신기를 초기화합니다...")
    
    # 2. 각 포트별로 독립적인 프로세스 생성 및 대기 상태 진입
    for conf in configs:
        p = mp.Process(target=collect_worker, args=(conf['rx_id'], conf['port'], conf['output'], BAUDRATE, DURATION, start_event, results))
        p.start()
        processes.append(p)

    # 3. 측정 위치로 이동할 준비 시간 (5초)
    print(f"\n[*] {PREP_TIME}초 뒤 수집을 시작합니다. 측정 위치로 이동하세요!")
    for i in range(PREP_TIME, 0, -1):
        print(f" - {i}초 전...")
        time.sleep(1)

    # 4. 수집 시작
    print("\n[>> 수집 시작 <<]")
    winsound.Beep(1000, 800)  # 1000Hz 주파수로 0.8초간 삐- 소리 발생
    start_event.set()         # 대기 중이던 4개의 프로세스에 동시 수집 시작 신호 전송

    # 5. 설정된 수집 시간만큼 대기
    # 워커 프로세스 내부에서 DURATION을 체크해 스스로 종료하므로, 여기서는 프로세스가 끝나길 기다림
    for p in processes:
        p.join()

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