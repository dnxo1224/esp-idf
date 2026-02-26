#!/usr/bin/env python3
# -*-coding:utf-8-*-

import sys
import csv
import json
import argparse
import serial
import threading
from io import StringIO
import time

DATA_COLUMNS_NAMES_C5C6 = ['type', 'id', 'mac', 'rssi', 'rate','noise_floor','fft_gain','agc_gain', 'channel', 'local_timestamp',  'sig_len', 'rx_state', 'len', 'first_word', 'data']
DATA_COLUMNS_NAMES = ['type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth', 'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding',
                      'sgi', 'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel', 'local_timestamp', 'ant', 'sig_len', 'rx_state', 'len', 'first_word', 'data']

is_running = True

def csi_data_read_parse(port: str, csv_writer, save_file_fd, log_file_fd):
    global is_running
    try:
        ser = serial.Serial(port=port, baudrate=921600, bytesize=8, parity='N', stopbits=1, timeout=1.0)
        if ser.isOpen():
            print(f'[+] Successfully opened port {port}')
        else:
            print(f'[-] Failed to open port {port}')
            return
    except serial.SerialException as e:
        print(f"[-] Error opening serial port {port}: {e}")
        return

    try:
        while is_running:
            line_bytes = ser.readline()
            if not line_bytes:
                continue

            try:
                strings = line_bytes.decode('utf-8', errors='ignore').strip()
            except Exception:
                continue

            if not strings:
                continue

            index = strings.find('CSI_DATA')

            if index == -1:
                log_file_fd.write(f"[{port}] {strings}\n")
                log_file_fd.flush()
                continue

            csv_reader = csv.reader(StringIO(strings))
            try:
                csi_data = next(csv_reader)
            except StopIteration:
                continue

            if len(csi_data) != len(DATA_COLUMNS_NAMES) and len(csi_data) != len(DATA_COLUMNS_NAMES_C5C6):
                continue

            try:
                csi_data_len = int(csi_data[-3])
                csi_raw_data = json.loads(csi_data[-1])
            except (ValueError, json.JSONDecodeError):
                continue
            
            if csi_data_len != len(csi_raw_data):
                continue

            # 해당 포트 전용 파일에 기록 (각자 독립된 파일이므로 Lock이 필요 없음)
            csv_writer.writerow(csi_data)
            # Add flush to ensure file writing immediately
            save_file_fd.flush()

    except Exception as e:
        print(f"[-] Serial read error on {port}: {e}")
    finally:
        ser.close()
        print(f"[*] Closed port {port}")

if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print('Python version should >= 3.6')
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Read CSI data from MULTIPLE serial ports and save them to SEPARATE CSV files simultaneously')
    parser.add_argument('-p', '--ports', dest='ports', nargs='+', required=True,
                        help='List of Serial port numbers (e.g., -p COM3 COM4 COM5)')
    parser.add_argument('--prefix', dest='prefix', action='store', default='csi_data',
                        help='Prefix for the output CSV files (e.g., csi_data_COM3.csv)')

    args = parser.parse_args()
    ports = args.ports
    prefix = args.prefix
    
    file_fds = []
    threads = []
    
    try:
        print(f"Preparing to listen on ports: {', '.join(ports)}...\n")
        
        # 각 포트별로 독립된 파일과 스레드를 생성합니다.
        for port in ports:
            # 특수 기호 제거 (예: /dev/ttyUSB0 -> dev_ttyUSB0)
            safe_port_name = port.replace('/', '_').replace('\\', '_')
            save_file_name = f"{prefix}_{safe_port_name}.csv"
            log_file_name = f"{prefix}_log_{safe_port_name}.txt"
            
            save_file_fd = open(save_file_name, 'w', newline='')
            log_file_fd = open(log_file_name, 'w')
            file_fds.extend([save_file_fd, log_file_fd])
            
            csv_writer = csv.writer(save_file_fd)
            # 헤더 기록
            csv_writer.writerow(DATA_COLUMNS_NAMES)
            
            print(f"[{port}] Target File: {save_file_name}")
            
            # 스레드 생성 (아직 시작 안 함)
            t = threading.Thread(target=csi_data_read_parse, args=(port, csv_writer, save_file_fd, log_file_fd))
            t.daemon = True
            threads.append(t)
            
        print("\nStarting all ports simultaneously... Press Ctrl+C to stop.\n")
        
        # 거의 동시에 모든 포트의 수신을 시작합니다.
        for t in threads:
            t.start()
            
        # 메인 스레드는 종료 대기
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nExiting. Stopping threads and closing serial ports...")
        is_running = False
        for t in threads:
            t.join(timeout=2.0)
    finally:
        for fd in file_fds:
            try:
                fd.close()
            except Exception:
                pass
