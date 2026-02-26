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

# 기존 데이터 열의 맨 앞에 'rx_port'를 추가하여 어떤 수신기에서 온 데이터인지 식별합니다.
DATA_COLUMNS_NAMES_C5C6 = ['rx_port', 'type', 'id', 'mac', 'rssi', 'rate','noise_floor','fft_gain','agc_gain', 'channel', 'local_timestamp',  'sig_len', 'rx_state', 'len', 'first_word', 'data']
DATA_COLUMNS_NAMES = ['rx_port', 'type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth', 'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding',
                      'sgi', 'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel', 'local_timestamp', 'ant', 'sig_len', 'rx_state', 'len', 'first_word', 'data']

# 여러 스레드가 동시에 CSV 파일에 접근하여 파일이 깨지는 것을 막기 위한 Lock 객체입니다.
csv_write_lock = threading.Lock()
is_running = True

def csi_data_read_parse(port: str, csv_writer, log_file_fd):
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
            strings = str(ser.readline())
            if not strings or strings == "b''":
                continue
            
            strings = strings.lstrip('b\'').rstrip('\\r\\n\'')
            index = strings.find('CSI_DATA')

            if index == -1:
                with csv_write_lock:
                    log_file_fd.write(f"[{port}] {strings}\n")
                    log_file_fd.flush()
                continue

            csv_reader = csv.reader(StringIO(strings))
            try:
                csi_data = next(csv_reader)
            except StopIteration:
                continue

            # csi_data 검증
            if len(csi_data) != (len(DATA_COLUMNS_NAMES) - 1) and len(csi_data) != (len(DATA_COLUMNS_NAMES_C5C6) - 1):
                continue

            try:
                csi_data_len = int(csi_data[-3])
                csi_raw_data = json.loads(csi_data[-1])
            except (ValueError, json.JSONDecodeError):
                continue
            
            if csi_data_len != len(csi_raw_data):
                continue

            # Lock을 걸고 안전하게 CSV의 새로운 row에 [포트명] + [CSI 데이터 배열]을 기록합니다.
            with csv_write_lock:
                csv_writer.writerow([port] + csi_data)

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
        description='Read CSI data from MULTIPLE serial ports and save it to ONE CSV file')
    # '+' 옵션을 사용하면 여러 개의 포트를 띄어쓰기로 입력받을 수 있습니다.
    parser.add_argument('-p', '--ports', dest='ports', nargs='+', required=True,
                        help='List of Serial port numbers (e.g., -p COM3 COM4 COM5)')
    parser.add_argument('-s', '--store', dest='store_file', action='store', default='multi_csi_data.csv',
                        help='Save the data from the serial port to a file')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='multi_csi_data_log.txt',
                        help='Save other serial data or bad CSI data to a log file')

    args = parser.parse_args()
    ports = args.ports
    
    try:
        save_file_fd = open(args.store_file, 'w', newline='')
        log_file_fd = open(args.log_file, 'w')
        
        csv_writer = csv.writer(save_file_fd)
        # 헤더 기록
        csv_writer.writerow(DATA_COLUMNS_NAMES)
        
        print(f"Listening on ports: {', '.join(ports)}...")
        print(f"Data will be saved to '{args.store_file}'")
        print("Press Ctrl+C to stop.\n")
        
        threads = []
        # 입력받은 각 포트마다 백그라운드 스레드를 생성하여 할당합니다.
        for port in ports:
            t = threading.Thread(target=csi_data_read_parse, args=(port, csv_writer, log_file_fd))
            t.daemon = True
            t.start()
            threads.append(t)
            
        # 메인 스레드는 종료(Ctrl+C)를 대기합니다.
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nExiting. Stopping threads and closing serial ports...")
        is_running = False
        # 스레드 종료 대기
        for t in threads:
            t.join(timeout=2.0)
    finally:
        try:
            save_file_fd.close()
            log_file_fd.close()
        except NameError:
            pass
