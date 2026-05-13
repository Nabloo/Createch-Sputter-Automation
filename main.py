import serial
import time
import csv
from datetime import datetime
from pathlib import Path


class JevaMetVCU:

    STATUS_CODES = {
        '0': 'OK',
        '1': 'Below range',
        '2': 'Above range',
        '3': 'Err Lo',
        '4': 'Err Hi',
        '5': 'Sensor off',
        '6': 'HV warming up',
        '7': 'Sensor error',
        '8': 'BA error',
        '9': 'No sensor',
        '10': 'No trigger point',
        '11': 'Pressure error',
        '12': 'Pirani error',
        '13': '24V supply error',
        '15': 'Filament broken'
    }

    def __init__(self, port='COM6', baudrate=19200):
        self.port = port
        self.baudrate = baudrate
        self.ser = None

    def connect(self):
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        print(f'Connected to {self.port}')

    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            print('Disconnected')

    def send_command(self, command, retries=3):
        for attempt in range(retries):
            try:
                full_cmd = f"{command}\r"
                self.ser.write(full_cmd.encode('ascii'))

                response = self.ser.read_until(b'\r')
                response = response.decode('ascii').strip()

                if response.startswith(command):
                    response = self.ser.read_until(b'\r')
                    response = response.decode('ascii').strip()

                if response:
                    return response

            except serial.SerialException as e:
                print(f'Serial error: {e}')

            time.sleep(0.1)

        return None

    def get_version(self):
        return self.send_command('RVN')

    def read_pressure(self, channel):
        response = self.send_command(f'RPV{channel}')

        if not response:
            return {
                'channel': channel,
                'status': 'No response',
                'pressure': None
            }

        return self.parse_pressure(channel, response)

    def parse_pressure(self, channel, response):
        parts = response.split(',')

        status_code = parts[0].strip()

        status = self.STATUS_CODES.get(
            status_code,
            f'Unknown ({status_code})'
        )

        pressure = None

        if status_code == '0' and len(parts) > 1:
            try:
                pressure = float(parts[1])
            except ValueError:
                pressure = None

        return {
            'channel': channel,
            'status': status,
            'pressure': pressure
        }

    def read_all_channels(self, channels=(1, 2, 3)):
        return [self.read_pressure(ch) for ch in channels]

    def get_pressure_unit(self):
        response = self.send_command('RGP').split(',')[0]
        unit_map = {
            '0': 'mbar',
            '1': 'Pa',
            '2': 'Torr'
        }

        return unit_map.get(response, 'unknown')


    def hv_on(self, channel=1):
        return self.send_command(f'SHV{channel},1')

    def hv_off(self, channel=1):
        return self.send_command(f'SHV{channel},0')


class CSVLogger:

    def __init__(self, log_dir='logs'):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def get_filepath(self):
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.log_dir / f'vcu_log_{date_str}.csv'

    def write(self, readings, pressure_unit):
        filepath = self.get_filepath()
        file_exists = filepath.exists()

        with open(filepath, 'a', newline='') as f:
            writer = csv.writer(f)

            if not file_exists:
                header = ['timestamp']

                for r in readings:
                    ch = r['channel']
                    header.extend([
                        f'ch{ch}_pressure_{pressure_unit}',
                        f'ch{ch}_status'
                    ])

                writer.writerow(header)

            row = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ]

            for r in readings:
                row.extend([
                    r['pressure'],
                    r['status']
                ])

            writer.writerow(row)


def main():
    vcu = JevaMetVCU(port='COM6')
    logger = CSVLogger()

    try:
        vcu.connect()
        version = vcu.get_version()
        print(f'Firmware: {version}')

        pressure_unit = vcu.get_pressure_unit()
        print(f'Pressure unit: {pressure_unit}')

        while True:
            readings = vcu.read_all_channels()
            print('-' * 60)

            for r in readings:
                print(
                    f"CH{r['channel']}: "
                    f"{r['pressure']} mbar | "
                    f"{r['status']}"
                )

            logger.write(readings, pressure_unit)
            time.sleep(1)

    except KeyboardInterrupt:
        print('Stopped')

    finally:
        vcu.disconnect()


if __name__ == '__main__':
    main()