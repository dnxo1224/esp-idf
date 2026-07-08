#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
Optimized CSI Data Collector
- 버퍼링된 파일 쓰기로 I/O 부하 감소
- 데이터 무결성 검증 추가
- 불필요한 print 제거
"""

import serial
import csv
import argparse
import sys
import os
import time


def collect_csi(port, output_file, baudrate=921600, duration=None):
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=0.1  # 더 짧은 타임아웃
        )
        # 시리얼 버퍼 크기 증가 (Windows/Linux)
        try:
            ser.set_buffer_size(rx_size=65536, tx_size=65536)
        except:
            pass  # 일부 플랫폼에서는 지원 안 됨
        
        print(f"[*] Connected to {port} at {baudrate} baud.")
    except serial.SerialException as e:
        print(f"[!] Failed to connect to {port}: {e}")
        sys.exit(1)

    print(f"[*] Saving CSI data to: {output_file}")
    print("[*] Waiting for CSI data... (Press Ctrl+C to stop)")

    # 버퍼링된 파일 쓰기 (65KB 버퍼)
    with open(output_file, 'w', newline='', buffering=65536) as csvfile:
        csv_writer = csv.writer(csvfile)
        header_written = False

        start_time = time.time()
        count = 0
        error_count = 0
        last_timestamp = 0

        try:
            while True:
                # 시간 제한 체크
                if duration is not None and (time.time() - start_time) >= duration:
                    print(f"\n[*] Collection stopped after {duration} seconds.")
                    break

                line_bytes = ser.readline()

                if not line_bytes:
                    continue

                try:
                    strings = line_bytes.decode('utf-8', errors='ignore').strip()
                except Exception:
                    error_count += 1
                    continue

                if not strings:
                    continue

                # 헤더 처리
                if "type,recv_mac,seq" in strings or "type,recv_mac,id" in strings:
                    if not header_written:
                        csv_writer.writerow(strings.split(','))
                        header_written = True
                        print("[+] Header saved.")
                    continue

                # CSI 데이터 처리
                if strings.startswith('CSI_DATA'):
                    row_data = strings.split(',')

                    # === 데이터 무결성 검증 ===
                    
                    # 1. 필드 개수 확인 (최소 25개 필드 필요)
                    if len(row_data) < 25:
                        error_count += 1
                        continue

                    # 2. timestamp 검증 (19번째 필드, 0-indexed)
                    try:
                        timestamp = int(row_data[19])
                        
                        # 비정상적으로 작은 timestamp 필터링
                        if timestamp < 1000:
                            error_count += 1
                            continue
                        
                        # timestamp 역순 감지 (리셋 제외)
                        if last_timestamp > 0 and timestamp < last_timestamp:
                            # 큰 점프면 리셋으로 간주, 작은 역순이면 에러
                            if last_timestamp - timestamp < 1000000:  # 1초 미만 역순
                                error_count += 1
                                continue
                        
                        last_timestamp = timestamp
                        
                    except (ValueError, IndexError):
                        error_count += 1
                        continue

                    # 3. RSSI 범위 확인 (4번째 필드)
                    try:
                        rssi = int(row_data[4])
                        if rssi > 0 or rssi < -100:
                            error_count += 1
                            continue
                    except (ValueError, IndexError):
                        error_count += 1
                        continue

                    # === 검증 통과, 저장 ===
                    csv_writer.writerow(row_data)
                    count += 1

                    # 100줄마다 flush 및 상태 출력
                    if count % 100 == 0:
                        csvfile.flush()
                        elapsed = time.time() - start_time
                        rate = count / elapsed if elapsed > 0 else 0
                        print(f"[+] {count} frames | {rate:.1f} Hz | errors: {error_count}")

        except KeyboardInterrupt:
            print("\n[*] Stopped by user.")
        except Exception as e:
            print(f"\n[!] Error: {e}")
        finally:
            # 최종 flush
            csvfile.flush()
            
            if ser.is_open:
                ser.close()

            # 최종 통계 출력
            elapsed = time.time() - start_time
            rate = count / elapsed if elapsed > 0 else 0
            print(f"\n{'='*30}")
            print(f"[*] Collection Summary")
            print(f" - Total frames: {count}")
            print(f" - Duration: {elapsed:.2f} seconds")
            print(f" - Average rate: {rate:.1f} Hz")
            print(f" - Errors filtered: {error_count}")
            print(f"{'='*30}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Optimized CSI Data Collector'
    )
    parser.add_argument(
        '-p', '--port',
        dest='port',
        required=True,
        help='Serial port (e.g., COM7 or /dev/ttyUSB0)'
    )
    parser.add_argument(
        '-o', '--output',
        dest='output_file',
        default='csi_data.csv',
        help='Output CSV file path'
    )
    parser.add_argument(
        '-b', '--baudrate',
        dest='baudrate',
        type=int,
        default=921600,
        help='Serial baudrate (default: 921600)'
    )
    parser.add_argument(
        '-t', '--duration',
        dest='duration',
        type=int,
        default=None,
        help='Collection duration in seconds'
    )

    args = parser.parse_args()

    # 출력 디렉토리 생성
    out_dir = os.path.dirname(args.output_file)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)

    collect_csi(
        port=args.port,
        output_file=args.output_file,
        baudrate=args.baudrate,
        duration=args.duration
    )