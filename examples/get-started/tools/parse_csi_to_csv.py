#!/usr/bin/env python3
# -*-coding:utf-8-*-

import sys
import csv
import json
import argparse
import serial
from os import path
from io import StringIO

DATA_COLUMNS_NAMES_C5C6 = ['type', 'id', 'mac', 'rssi', 'rate','noise_floor','fft_gain','agc_gain', 'channel', 'local_timestamp',  'sig_len', 'rx_state', 'len', 'first_word', 'data']
DATA_COLUMNS_NAMES = ['type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth', 'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding',
                      'sgi', 'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel', 'local_timestamp', 'ant', 'sig_len', 'rx_state', 'len', 'first_word', 'data']

def csi_data_read_parse(port: str, csv_writer, log_file_fd):
    try:
        ser = serial.Serial(port=port, baudrate=921600, bytesize=8, parity='N', stopbits=1)
        if ser.isOpen():
            print(f'Successfully opened port {port}')
        else:
            print(f'Failed to open port {port}')
            return
    except serial.SerialException as e:
        print(f"Error opening serial port: {e}")
        return

    try:
        while True:
            strings = str(ser.readline())
            if not strings:
                break
            
            # Remove line endings and byte string prefix/suffix
            strings = strings.lstrip('b\'').rstrip('\\r\\n\'')
            
            # Find the line containing CSI data
            index = strings.find('CSI_DATA')

            if index == -1:
                # If it's not CSI data, write it to the log file (e.g., debug messages)
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                continue

            # Parse the CSV string
            csv_reader = csv.reader(StringIO(strings))
            try:
                csi_data = next(csv_reader)
            except StopIteration:
                continue

            # Basic validation
            if len(csi_data) != len(DATA_COLUMNS_NAMES) and len(csi_data) != len(DATA_COLUMNS_NAMES_C5C6):
                log_file_fd.write('element number is not equal\n')
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                continue

            try:
                csi_data_len = int(csi_data[-3])
                csi_raw_data = json.loads(csi_data[-1])
            except (ValueError, json.JSONDecodeError):
                log_file_fd.write('data is incomplete or invalid format\n')
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                continue
            
            if csi_data_len != len(csi_raw_data):
                log_file_fd.write('csi_data_len is not equal\n')
                log_file_fd.write(strings + '\n')
                log_file_fd.flush()
                continue

            # Write the valid CSI data row to the CSV file
            csv_writer.writerow(csi_data)

    except KeyboardInterrupt:
        print("\nExiting. Closing serial port.")
    finally:
        ser.close()

if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print('Python version should >= 3.6')
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Read CSI data from serial port and save it to a CSV file')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='Serial port number of csi_recv device (e.g., COM3, /dev/ttyUSB0)')
    parser.add_argument('-s', '--store', dest='store_file', action='store', default='csi_data.csv',
                        help='Save the data from the serial port to a file')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='csi_data_log.txt',
                        help='Save other serial data or bad CSI data to a log file')

    args = parser.parse_args()
    
    try:
        save_file_fd = open(args.store_file, 'w', newline='')
        log_file_fd = open(args.log_file, 'w')
        
        csv_writer = csv.writer(save_file_fd)
        # Write header (we use the longer format by default, you can adjust if using C5/C6 specifically)
        csv_writer.writerow(DATA_COLUMNS_NAMES)
        
        print(f"Listening on {args.port}... Press Ctrl+C to stop.")
        print(f"Data will be saved to '{args.store_file}' and logs to '{args.log_file}'")
        
        csi_data_read_parse(args.port, csv_writer, log_file_fd)
        
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        try:
            save_file_fd.close()
            log_file_fd.close()
        except NameError:
            pass
