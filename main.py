import serial
import time
import csv
import os
from datetime import datetime

# Configure these to match your setup
PORT = 'COM6'      # Change to your COM port
BAUD = 19200       # Match your VCU setting
LOG_DIR = 'logs'   # Directory to save log files

def send_command(ser, command):
    """Send a command and return the response."""
    ser.write((command + '\r').encode('ascii'))
    time.sleep(0.1)
    response = ser.readline().decode('ascii').strip()
    return response

def parse_pressure(response):
    """Parse the RPV response into status and pressure value."""
    parts = response.split(',')
    status_codes = {
        '0': 'OK',
        '1': 'Below range',
        '2': 'Above range',
        '3': 'Err Lo',
        '4': 'Err Hi',
        '5': 'Sensor off',
        '6': 'HV on (warming up)',
        '7': 'Sensor error',
        '8': 'BA error',
        '9': 'No sensor',
        '10': 'No trigger point',
        '11': 'Pressure error',
        '12': 'Pirani error',
        '13': '24V supply error',
        '15': 'Filament broken'
    }

    status_num = parts[0].strip()
    status = status_codes.get(status_num, f'Unknown status {status_num}')

    if status_num == '0':
        pressure = parts[1].strip() if len(parts) > 1 else 'N/A'
    else:
        pressure = 'N/A'

    return status, pressure

def get_log_filepath():
    """Return the log file path for today's date."""
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(LOG_DIR, f'vcu_log_{date_str}.csv')

def write_header_if_needed(filepath):
    """Write CSV header if the file doesn't exist yet."""
    if not os.path.exists(filepath):
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Timestamp',
                'CH1 Pressure', 'CH1 Status',
                'CH2 Pressure', 'CH2 Status',
                'CH3 Pressure', 'CH3 Status'
            ])

def log_reading(timestamp, readings):
    """Append a reading to today's log file."""
    filepath = get_log_filepath()
    write_header_if_needed(filepath)

    with open(filepath, 'a', newline='') as f:
        writer = csv.writer(f)
        row = [timestamp]
        for status, pressure in readings:
            row.extend([pressure, status])
        writer.writerow(row)

def read_all_channels(ser):
    """Read pressure from all 3 channels, return list of (status, pressure) tuples."""
    readings = []
    for channel in range(1, 4):
        response = send_command(ser, f'RPV{channel}')
        if response:
            status, pressure = parse_pressure(response)
        else:
            status, pressure = 'No response', 'N/A'
        readings.append((status, pressure))
    return readings

def print_readings(timestamp, readings):
    """Print readings to console."""
    print("-" * 50)
    print(f'Timestamp: {timestamp}')
    for i, (status, pressure) in enumerate(readings, start=1):
        print(f'  Channel {i}: {pressure} mbar  (Status: {status})')

def main():
    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=BAUD,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=1
        )
        print(f'Connected to {PORT} at {BAUD} baud')

        version = send_command(ser, 'RVN')
        print(f'VCU firmware version: {version}')
        print(f'Logging to: {LOG_DIR}/')
        print()
        print('Reading channels (Ctrl+C to stop)...')

        while True:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            readings = read_all_channels(ser)
            print_readings(timestamp, readings)
            log_reading(timestamp, readings)
            time.sleep(1)

    except serial.SerialException as e:
        print(f'Serial port error: {e}')
    except KeyboardInterrupt:
        print('\nStopped by user')
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
            print('Port closed')

if __name__ == '__main__':
    main()