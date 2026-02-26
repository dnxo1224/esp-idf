#!/usr/bin/env python3
# -*-coding:utf-8-*-

import serial
import csv
import argparse
import sys
import os
import time

def collect_csi(port, output_file, baudrate=921600):
    try:
        # Added timeout so readline() doesn't block forever if no data comes
        ser = serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity='N', stopbits=1, timeout=1.0)
        print(f"[*] Successfully connected to {port} at {baudrate} baud.")
    except serial.SerialException as e:
        print(f"[!] Failed to connect to {port}: {e}")
        sys.exit(1)

    print(f"[*] Saving CSI data to: {output_file}")
    print("[*] Waiting for CSI data... (Press Ctrl+C to stop)")

    with open(output_file, 'w', newline='') as csvfile:
        csv_writer = csv.writer(csvfile)
        header_written = False

        try:
            while True:
                line_bytes = ser.readline()
                
                # If timeout occurred, just continue looping (allows Ctrl+C to be caught)
                if not line_bytes:
                    continue
                
                try:
                    # Decode bytes to string
                    strings = line_bytes.decode('utf-8', errors='ignore').strip()
                except Exception as e:
                    print(f"[DEBUG] Decode error: {e}. Raw bytes: {line_bytes}")
                    continue

                if not strings:
                    continue

                # ================= DEBUG PRINT =================
                # Let's print EVERYTHING the ESP32 is sending
                print(f"[RAW RX] {strings}")
                # ===============================================

                # Look for the CSV Header from csi_recv
                if "type,recv_mac,seq" in strings or "type,recv_mac,id" in strings:
                    if not header_written:
                        print("\n[+] -------- Header matched! Saving. --------\n")
                        csv_writer.writerow(strings.split(','))
                        header_written = True
                    continue

                # Look for the actual data payload
                if strings.startswith('CSI_DATA'):
                    row_data = strings.split(',')
                    csv_writer.writerow(row_data)
                    csvfile.flush() # Force write to disk
                    print(f"[+] Saved CSI frame: len={len(row_data)} elements")

        except KeyboardInterrupt:
            print("\n[*] Collection stopped by user. File saved.")
        except Exception as e:
            print(f"\n[!] Unexpected error occurred: {e}")
        finally:
            if ser.is_open:
                ser.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read CSI data from serial port and save to CSV (Lightweight)')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='Serial port number (e.g. COM7)')
    parser.add_argument('-o', '--output', dest='output_file', action='store', default='csi_measured_data.csv',
                        help='Path to save the output CSV file')
    parser.add_argument('-b', '--baudrate', dest='baudrate', action='store', type=int, default=921600,
                        help='Serial baudrate (default: 921600 for esp-csi)')

    args = parser.parse_args()

    # Create directory if saving to a deeper path
    out_dir = os.path.dirname(args.output_file)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)

    collect_csi(args.port, args.output_file, args.baudrate)

