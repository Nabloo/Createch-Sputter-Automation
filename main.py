import serial
import time
import csv
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

matplotlib.use('TkAgg')

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

    # ----------------------------------------------------------
    def connect(self):
        """Open the serial port."""
        try:
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
        except serial.SerialException as e:
            raise RuntimeError(f'Could not open {self.port}: {e}')

    # ----------------------------------------------------------
    def disconnect(self):
        """Close the serial port if open."""
        if self.ser and self.ser.is_open:
            self.ser.close()
            print('Disconnected')

    # ----------------------------------------------------------
    def send_command(self, command, retries=3):
        """Write a command, read the reply, retry on failure."""
        for _ in range(retries):
            try:
                full_cmd = f"{command}\r"
                self.ser.write(full_cmd.encode('ascii'))
                self.ser.flush()
                response = self.ser.read_until(b'\r')
                response = response.decode('ascii').strip()
                # Some VCU commands echo the command first → skip it
                if response.startswith(command):
                    response = self.ser.read_until(b'\r').decode('ascii').strip()
                if response:
                    return response
            except serial.SerialException as e:
                print(f'Serial error: {e}')
            time.sleep(0.1)
        return None

    # ----------------------------------------------------------
    def get_version(self):
        return self.send_command('RVN')

    # ----------------------------------------------------------
    def read_pressure(self, channel):
        resp = self.send_command(f'RPV{channel}')
        if not resp:
            return {'channel': channel, 'status': 'No response', 'pressure': None}
        return self.parse_pressure(channel, resp)

    # ----------------------------------------------------------
    def parse_pressure(self, channel, response):
        parts = response.split(',')
        status_code = parts[0].strip()
        status = self.STATUS_CODES.get(status_code,
                                       f'Unknown ({status_code})')
        pressure = None
        if status_code == '0' and len(parts) > 1:
            try:
                pressure = float(parts[1])
            except ValueError:
                pressure = None
        return {'channel': channel, 'status': status, 'pressure': pressure}

    # ----------------------------------------------------------
    def read_all_channels(self, channels=(1, 2, 3)):
        return [self.read_pressure(ch) for ch in channels]

    # ----------------------------------------------------------
    def get_pressure_unit(self):
        """
        0 → mbar, 1 → Torr, 2 → Pa
        """
        resp = self.send_command('RGP').split(',')[0]

        return {'0': 'mbar', '1': 'Torr', '2': 'Pa'}.get(resp, 'unknown')

    # ----------------------------------------------------------
    def hv_on(self, channel=1):
        return self.send_command(f'SHV{channel},1')

    def hv_off(self, channel=1):
        return self.send_command(f'SHV{channel},0')


# --------------------------------------------------------------
# 3️⃣  CSV logger (unchanged)
# --------------------------------------------------------------
class CSVLogger:
    def __init__(self, log_dir='logs'):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def get_filepath(self):
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.log_dir / (f'MVC-3_log_{date_str}.csv')

    def write(self, readings, pressure_unit):
        filepath = self.get_filepath()
        file_exists = filepath.exists()
        with open(filepath, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                header = ['timestamp']
                for r in readings:
                    ch = r['channel']
                    header.extend([f'ch{ch}_pressure_{pressure_unit}',
                                   f'ch{ch}_status'])
                writer.writerow(header)
            row = [datetime.now().strftime('%Y-%m-%d %H:%M:%S')]
            for r in readings:
                row.extend([r['pressure'], r['status']])
            writer.writerow(row)


# --------------------------------------------------------------
#  Custom toolbar – catches the Home button
# --------------------------------------------------------------
class MyToolbar(NavigationToolbar2Tk):
    """Toolbar that notifies the GUI when the Home button is pressed."""
    def __init__(self, canvas, window, gui):
        super().__init__(canvas, window)
        self.gui = gui                     # reference to the VCUGui instance

    def home(self):
        """Reset view and tell the GUI to re‑enable auto‑scroll."""
        super().home()                    # perform the normal Matplotlib home action
        self.gui._reset_autoscroll()      # tell VCUGui that scrolling may continue


# --------------------------------------------------------------
#  VCUGui – Tkinter window with scrollable / zoomable plot
# --------------------------------------------------------------
class VCUGui(tk.Tk):
    UPDATE_MS = 1000          # measurement interval [ms]

    def __init__(self, vcu: JevaMetVCU, logger: CSVLogger):
        super().__init__()
        self.title("JEVAmet® VCU – Pressure Monitor")
        self.geometry("940x660")
        self.resizable(False, False)

        self.vcu = vcu
        self.logger = logger

        self.running = False          # acquisition flag
        self.unit = "unknown"
        self.start_time = None

        self.autoscroll = True        # True → x‑axis follows new data
        self._updating_limits = False # internal flag to separate user vs code changes

        self._build_widgets()
        self._init_plot()

    # ------------------------------------------------------
    def _build_widgets(self):
        # ----- Control panel -----
        ctrl = ttk.LabelFrame(self, text="Control")
        ctrl.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(ctrl, text="Start", command=self._toggle)
        self.btn_start.pack(side="left", padx=5, pady=5)

        self.lbl_conn = ttk.Label(ctrl, text="Disconnected", foreground="red")
        self.lbl_conn.pack(side="left", padx=10)

        # ----- Current readings -----
        read = ttk.LabelFrame(self, text="Current Readings")
        read.pack(fill="x", padx=10, pady=5)

        self.var_ch = {}
        for ch in (1, 2, 3):
            var = tk.StringVar(value=f"CH{ch}: – –")
            lbl = ttk.Label(read, textvariable=var, width=30, anchor="w")
            lbl.pack(side="left", padx=5)
            self.var_ch[ch] = var

        # ----- Plot area -----
        plot_fr = ttk.LabelFrame(self, text="Pressure Plot")
        plot_fr.pack(fill="both", expand=True, padx=10, pady=5)

        self.fig, self.ax = plt.subplots(figsize=(8.5, 4), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_fr)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # custom toolbar with Home‑button detection
        self.toolbar = MyToolbar(self.canvas, self, self)
        self.toolbar.update()

    # ------------------------------------------------------
    def _init_plot(self):
        """Create empty lines and connect the x‑limit callback."""
        self.time_data = []                 # seconds from start
        self.press_data = {1: [], 2: [], 3: []}
        self.lines = {}
        colours = {1: "tab:blue", 2: "tab:orange", 3: "tab:green"}

        for ch in (1, 2, 3):
            line, = self.ax.plot([], [], label=f"CH{ch}", color=colours[ch])
            self.lines[ch] = line

        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Pressure")
        self.ax.grid(True, which="both", ls="--", lw=0.5)
        self.ax.legend()
        self.fig.tight_layout()

        # Detect manual zoom/pan → stop auto‑scroll
        self.ax.callbacks.connect('xlim_changed', self._on_xlim_changed)

    # ------------------------------------------------------
    def _on_xlim_changed(self, ax):
        """User changed the X limits (zoom/pan); pause auto‑scroll."""
        if self._updating_limits:
            return                     # change originated from our own code
        self.autoscroll = False

    # ------------------------------------------------------
    def _reset_autoscroll(self):
        """Called by the toolbar Home button – re‑enable scrolling."""
        self.autoscroll = True

    # ------------------------------------------------------
    def _toggle(self):
        """Start or stop measurement."""
        if not self.running:
            try:
                self.vcu.connect()
                self.unit = self.vcu.get_pressure_unit()
                self.start_time = time.time()
                self.time_data.clear()
                for ch in self.press_data:
                    self.press_data[ch].clear()
                self.btn_start.config(text="Stop")
                self.lbl_conn.config(text=f"Connected – {self.unit}",
                                     foreground="green")
                self.running = True
                self.after(self.UPDATE_MS, self._acquire)
            except Exception as e:
                messagebox.showerror("Connection error", str(e))
        else:
            self.running = False
            self.vcu.disconnect()
            self.btn_start.config(text="Start")
            self.lbl_conn.config(text="Disconnected", foreground="red")

    # ------------------------------------------------------
    def _acquire(self):
        """Read data, update UI & plot, then schedule the next call."""
        if not self.running:
            return

        # 1️⃣ Read all channels and log them
        readings = self.vcu.read_all_channels()
        self.logger.write(readings, self.unit)

        # 2️⃣ Update numeric labels
        for r in readings:
            ch = r["channel"]
            pressure = (f"{r['pressure']:.3g}"
                        if r["pressure"] is not None else "—")
            self.var_ch[ch].set(f"CH{ch}: {pressure} {self.unit} | {r['status']}")

        # 3️⃣ Append data to the plot
        elapsed = time.time() - self.start_time
        self.time_data.append(elapsed)
        for r in readings:
            ch = r["channel"]
            self.press_data[ch].append(r["pressure"]
                                        if r["pressure"] is not None
                                        else float("nan"))
            self.lines[ch].set_data(self.time_data, self.press_data[ch])

        # 4️⃣ Auto‑scroll handling
        if self.autoscroll:
            self._updating_limits = True
            # full view from start (or use a moving window, e.g. elapsed‑30)
            self.ax.set_xlim(0, elapsed)
            self._updating_limits = False

        # 5️⃣ Redraw
        self.ax.relim()
        self.ax.autoscale_view(scalex=False)   # keep x‑limits as we set them
        self.canvas.draw_idle()

        # 6️⃣ Schedule next measurement
        self.after(self.UPDATE_MS, self._acquire)


# --------------------------------------------------------------
# 5️⃣  Main entry point
# --------------------------------------------------------------
def main():
    vcu = JevaMetVCU(port='COM6')   # <-- adjust if you use another COM port
    logger = CSVLogger()
    app = VCUGui(vcu, logger)
    app.mainloop()


if __name__ == '__main__':
    main()

