import serial
import time

# Configure these to match your setup
PORT = 'COM6'  # Change to your COM port
BAUD = 19200  # Match your VCU setting


def send_command(ser, command):
    """Send a command and return the response."""
    ser.write((command + '\r').encode('ascii'))
    time.sleep(0.1)  # Give the VCU time to respond
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


def read_all_channels(ser):
    """Read pressure from all 3 channels."""
    print("-" * 40)
    for channel in range(1, 4):
        response = send_command(ser, f'RPV{channel}')
        if response:
            status, pressure = parse_pressure(response)
            print(f'Channel {channel}: {pressure} mbar  (Status: {status})')
        else:
            print(f'Channel {channel}: No response')
    print("-" * 40)


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

        # First check firmware version
        version = send_command(ser, 'RVN')
        print(f'VCU firmware version: {version}')
        print()

        # Continuously read all channels
        print('Reading channels (Ctrl+C to stop)...')
        while True:
            read_all_channels(ser)
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