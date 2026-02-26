#!/usr/bin/env python3
# -*-coding:utf-8-*-

import serial
import csv
import argparse
import sys
import os
import threading
import time

is_running = True

def collect_csi_thread(port, output_file, baudrate=921600):
    global is_running
    try:
        # Added timeout so readline() doesn't block forever if no data comes
        ser = serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity='N', stopbits=1, timeout=1.0)
        print(f"[*] Successfully connected to {port} at {baudrate} baud.")
    except serial.SerialException as e:
        print(f"[!] Failed to connect to {port}: {e}")
        return

    print(f"[*] Saving {port} CSI data to: {output_file}")

    with open(output_file, 'w', newline='') as csvfile:
        csv_writer = csv.writer(csvfile)
        header_written = False

        try:
            while is_running:
                line_bytes = ser.readline()
                
                # If timeout occurred, just continue looping (allows Ctrl+C to be caught)
                if not line_bytes:
                    continue
                
                try:
                    # Decode bytes to string
                    strings = line_bytes.decode('utf-8', errors='ignore').strip()
                except Exception as e:
                    # print(f"[DEBUG {port}] Decode error: {e}")
                    continue

                if not strings:
                    continue

                # ================= DEBUG PRINT =================
                # Let's print EVERYTHING the ESP32 is sending
                # print(f"[RAW RX {port}] {strings}")
                # ===============================================

                # Look for the CSV Header from csi_recv
                if "type,recv_mac,seq" in strings or "type,recv_mac,id" in strings:
                    if not header_written:
                        print(f"\n[+] -------- [{port}] Header matched! Saving. --------\n")
                        csv_writer.writerow(strings.split(','))
                        header_written = True
                    continue

                # Look for the actual data payload
                if strings.startswith('CSI_DATA'):
                    row_data = strings.split(',')
                    csv_writer.writerow(row_data)
                    csvfile.flush() # Force write to disk
                    print(f"[{port}] Saved CSI frame: len={len(row_data)} elements")

        except Exception as e:
            print(f"\n[!] Unexpected error occurred on {port}: {e}")
        finally:
            if ser.is_open:
                ser.close()
                print(f"[*] Closed port {port}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read CSI data from MULTIPLE serial ports and save to SEPARATE CSVs (Based on collect_csi)')
    parser.add_argument('-p', '--ports', dest='ports', nargs='+', required=True,
                        help='List of Serial port numbers (e.g., -p COM3 COM4 COM5)')
    parser.add_argument('-o', '--output', dest='output_file', action='store', default='csi_measured_data.csv',
                        help='Base output name (will append port names like csi_measured_data_COM3.csv)')
    parser.add_argument('-b', '--baudrate', dest='baudrate', action='store', type=int, default=921600,
                        help='Serial baudrate (default: 921600 for esp-csi)')

    args = parser.parse_args()
    
    # Explicitly cast to string to fix Pyright / Pylance type checker errors 
    # about potentially mixing bytes and str in os.path.join
    output_file = str(args.output_file)
    out_dir = os.path.dirname(output_file)
    
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)

    base_name = os.path.basename(output_file)
    name_parts = os.path.splitext(base_name)
    prefix = name_parts[0]
    ext = name_parts[1] if len(name_parts) > 1 else '.csv'

    print(f"Preparing to listen on ports: {', '.join(args.ports)}...\n")
    print("[*] Waiting for CSI data... (Press Ctrl+C to stop)")

    threads = []
    
    try:
        # 각 포트별로 스레드와 파일 이름을 할당합니다.
        for port in args.ports:
            # Ensure port is a string so string methods don't raise type warnings
            port_str = str(port)
            safe_port_name = port_str.replace('/', '_').replace('\\', '_')
            
            # Since out_dir is definitely a str, this join will pass type checking
            save_file_name = os.path.join(out_dir if out_dir else '.', f"{prefix}_{safe_port_name}{ext}")
            
            # 스레드 생성 (collect_csi_thread 호출)
            t = threading.Thread(target=collect_csi_thread, args=(port, save_file_name, args.baudrate))
            t.daemon = True
            t.start()
            threads.append(t)
            
        # 메인 스레드는 종료 대기 (Ctrl+C 입력을 기다림)
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n[*] Collection stopped by user. Stopping threads and saving files...")
        is_running = False
        # 스레드가 루프를 빠져나오고 파일을 닫을 시간을 줍니다
        for t in threads:
            t.join(timeout=2.0)
    finally:
        print("Done.")
