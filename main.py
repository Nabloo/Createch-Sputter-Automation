import serial
import time
import csv
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from serial.tools import list_ports

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

matplotlib.use('TkAgg')

# --------------------------------------------------------------
# DEVICE REGISTRY
# --------------------------------------------------------------
AVAILABLE_DEVICES = {
    "VCU": {
        "channels": [1, 2, 3]
    }
}

class MVC_3:
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
    UPDATE_MS = 1000

    def __init__(self, vcu: MVC_3, logger: CSVLogger):

        super().__init__()
        self.title("Createch Sputter readout")
        self.geometry("1400x700")
        self.resizable(True, True)
        self.vcu = vcu
        self.logger = logger
        self.running = False
        self.unit = "unknown"
        self.start_time = None
        self.autoscroll = True
        self._updating_limits = False
        self.time_data = []
        self.press_data = {
            1: [],
            2: [],
            3: []
        }

        self._build_widgets()

    # ------------------------------------------------------
    def _build_widgets(self):
        # -------------------------------
        # Connection / Start controls
        # -------------------------------
        top_bar = ttk.Frame(self)
        top_bar.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(
            top_bar,
            text="Start",
            command=self._toggle_measurement
        )
        self.btn_start.pack(side="left", padx=5)

        self.lbl_conn = ttk.Label(
            top_bar,
            text="Disconnected",
            foreground="red"
        )
        self.lbl_conn.pack(side="left", padx=10)

        # -------------------------------
        # Multi-panel container
        # -------------------------------
        self.panel_container = ttk.Frame(self)

        self.panel_container.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=5
        )

        self.panels = []
        self.add_panel() # start with ONE panel only

        # -------------------------------
        # Connection Settings (COM / Baudrate)
        # -------------------------------
        conn_frame = ttk.LabelFrame(self, text="Connection Settings")
        conn_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(conn_frame, text="COM Port:").pack(side="left", padx=5)

        self.com_var = tk.StringVar()

        self.com_dropdown = ttk.Combobox(
            conn_frame,
            textvariable=self.com_var,
            values=self.get_available_ports(),
            state="readonly",
            width=12
        )
        self.com_dropdown.pack(side="left", padx=5)

        # refresh button for COM
        ttk.Button(
            conn_frame,
            text="⟳",
            width=3,
            command=self.refresh_ports
        ).pack(side="left", padx=5)

        ttk.Label(conn_frame, text="Baudrate:").pack(side="left", padx=10)

        self.baud_var = tk.StringVar(value=str(self.vcu.baudrate))

        self.entry_baud = ttk.Entry(
            conn_frame,
            textvariable=self.baud_var,
            width=10
        )
        self.entry_baud.pack(side="left", padx=5)

        ports = self.get_available_ports()
        self.com_dropdown["values"] = ports

        # default selection priority: COM6 → first available → empty
        if "COM6" in ports:
            self.com_var.set("COM6")
        elif ports:
            self.com_var.set(ports[0])
        else:
            self.com_var.set("")


        # -------------------------------
        # Current readings
        # -------------------------------
        read = ttk.LabelFrame(self, text="Current Readings")

        read.pack(fill="x", padx=10, pady=5)

        self.var_ch = {}

        for ch in (1, 2, 3):
            var = tk.StringVar(value=f"CH{ch}: – –")

            lbl = ttk.Label(
                read,
                textvariable=var,
                width=30,
                anchor="w"
            )

            lbl.pack(side="left", padx=5)

            self.var_ch[ch] = var

    def refresh_ports(self):
        ports = self.get_available_ports()
        self.com_dropdown["values"] = ports

        # auto-select first port if available and nothing selected
        if ports and not self.com_var.get():
            self.com_var.set(ports[0])

    def add_panel(self):
        panel_id = len(self.panels) + 1

        panel = DevicePanel(
            self.panel_container,
            self,
            panel_id,
            on_close=self.remove_specific_panel
        )

        self.panels.append(panel)
        self.relayout_panels()

    def remove_specific_panel(self, panel):
        # prevent closing last remaining panel
        if len(self.panels) <= 1:
            return

        if panel in self.panels:
            self.panels.remove(panel)
            panel.destroy()
            self.relayout_panels()

    def relayout_panels(self):

        for widget in self.panel_container.winfo_children():
            widget.grid_forget()

        for i, panel in enumerate(self.panels):
            panel.grid(
                row=0,
                column=i,
                sticky="nsew",
                padx=5,
                pady=5
            )

        for i in range(len(self.panels)):
            self.panel_container.columnconfigure(i, weight=1)

        self.update_panel_buttons()

    def update_panel_buttons(self):
        for i, panel in enumerate(self.panels):
            is_last = (i == len(self.panels) - 1)

            if hasattr(panel, "btn_add_panel"):
                if is_last:
                    panel.btn_add_panel.pack(side="right", padx=(2, 0))
                else:
                    panel.btn_add_panel.pack_forget()

    def get_available_ports(self):
        ports = list_ports.comports()
        return [p.device for p in ports]

    # ------------------------------------------------------
    def _init_plot(self):
        """Create empty lines and connect the x‑limit callback."""
        self.time_data = []                 # seconds from start
        self.press_data = {1: [], 2: [], 3: []}
        self.lines = {}
        colours = {1: "tab:blue", 2: "tab:orange", 3: "tab:green"}

        for channel in (1, 2, 3):
            line, = self.ax.plot([], [], label=f"Channel {channel}", color=colours[channel])
            self.lines[channel] = line

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
    def _toggle_measurement(self):

        if not self.running:

            try:
                # update connection settings from UI
                self.vcu.port = self.com_var.get()

                if not self.vcu.port:
                    messagebox.showerror("No COM port", "Please select a COM port")
                    return

                try:
                    self.vcu.baudrate = int(self.baud_var.get())
                except ValueError:
                    messagebox.showerror("Invalid baudrate", "Baudrate must be an integer")
                    return

                self.vcu.connect()

                self.unit = self.vcu.get_pressure_unit()
                self.start_time = time.time()
                self.time_data.clear()

                for ch in self.press_data:
                    self.press_data[ch].clear()

                self.btn_start.config(text="Stop")

                self.lbl_conn.config(
                    text="Connected",
                    foreground="green"
                )

                self.running = True

                self.after(
                    self.UPDATE_MS,
                    self._acquire
                )

            except Exception as e:

                messagebox.showerror(
                    "Connection error",
                    str(e)
                )

        else:

            self.running = False

            self.vcu.disconnect()

            self.btn_start.config(text="Start")

            self.lbl_conn.config(
                text="Disconnected",
                foreground="red"
            )

    # ------------------------------------------------------
    def _acquire(self):

        if not self.running:
            return

        readings = self.vcu.read_all_channels()

        self.logger.write(
            readings,
            self.unit
        )

        for r in readings:
            channel = r["channel"]

            pressure = (
                f"{r['pressure']:.3g}"
                if r["pressure"] is not None
                else "—"
            )

            self.var_ch[channel].set(
                f"CH{channel}: "
                f"{pressure} {self.unit} | "
                f"{r['status']}"
            )

        elapsed = time.time() - self.start_time

        self.time_data.append(elapsed)

        for r in readings:
            channel = r["channel"]

            self.press_data[channel].append(
                r["pressure"]
                if r["pressure"] is not None
                else float("nan")
            )

        # -------------------------------
        # Update all panels
        # -------------------------------
        for panel in self.panels:
            panel.update_plot(
                self.time_data,
                self.press_data,
                self.unit
            )

        self.after(
            self.UPDATE_MS,
            self._acquire
        )

class DevicePanel(ttk.LabelFrame):
    def __init__(self, parent, gui, panel_id, on_close=None):
        super().__init__(parent)

        self.gui = gui
        self.on_close = on_close
        self.device_var = tk.StringVar(value="VCU")
        self.channel_vars = {}

        # -------------------------------
        # Device row (with + and X buttons)
        # -------------------------------
        dev_frame = ttk.Frame(self)
        dev_frame.pack(fill="x", padx=5, pady=5)

        ttk.Label(dev_frame, text="Device:").pack(side="left")

        self.device_dropdown = ttk.Combobox(
            dev_frame,
            textvariable=self.device_var,
            values=list(AVAILABLE_DEVICES.keys()),
            state="readonly",
            width=15
        )
        self.device_dropdown.pack(side="left", padx=5)

        # flexible spacer pushes buttons right
        spacer = ttk.Frame(dev_frame)
        spacer.pack(side="left", expand=True, fill="x")

        # "+" button (only shown for last panel later)
        self.btn_add_panel = ttk.Button(
            dev_frame,
            text="+",
            width=3,
            command=self.gui.add_panel
        )
        self.btn_add_panel.pack(side="right", padx=(2, 0))

        # "X" close button
        ttk.Button(
            dev_frame,
            text="✕",
            width=3,
            command=self.close_panel
        ).pack(side="right", padx=(2, 5))

        # -------------------------------
        # Channel selection
        # -------------------------------
        ch_frame = ttk.Frame(self)
        ch_frame.pack(fill="x", padx=5, pady=5)

        ttk.Label(ch_frame, text="Channels:").pack(side="left")

        for ch in AVAILABLE_DEVICES["VCU"]["channels"]:

            var = tk.BooleanVar(value=True)

            cb = ttk.Checkbutton(
                ch_frame,
                text=f"CH{ch}",
                variable=var,
                command=self.update_channel_visibility
            )

            cb.pack(side="left", padx=2)

            self.channel_vars[ch] = var

        # -------------------------------
        # Plot
        # -------------------------------
        self.fig, self.ax = plt.subplots(figsize=(4.5, 3), dpi=100)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)

        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.toolbar = MyToolbar(self.canvas, self, gui)

        self.toolbar.update()

        self.lines = {}

        colours = {
            1: "tab:blue",
            2: "tab:orange",
            3: "tab:green"
        }

        for ch in AVAILABLE_DEVICES["VCU"]["channels"]:

            line, = self.ax.plot(
                [],
                [],
                label=f"CH{ch}",
                color=colours[ch]
            )

            self.lines[ch] = line

        self.ax.grid(True, which="both", ls="--", lw=0.5)

        self.ax.legend()

        self.ax.set_xlabel("Time (s)")

        self.ax.set_ylabel("Pressure")

        self.fig.tight_layout()

    def close_panel(self):
        if self.on_close:
            self.on_close(self)

    # ------------------------------------------------------
    def update_channel_visibility(self):

        for ch, line in self.lines.items():
            line.set_visible(
                self.channel_vars[ch].get()
            )

        self.rescale_y_axis()

        self.ax.legend()

        self.canvas.draw_idle()

    # ------------------------------------------------------
    def update_plot(self, time_data, pressure_data, unit):

        self.ax.set_ylabel(f"Pressure [{unit}]")

        visible_lines = False

        for ch, line in self.lines.items():

            line.set_data(
                time_data,
                pressure_data[ch]
            )

            if self.channel_vars[ch].get():
                visible_lines = True

        self.ax.relim()

        self.ax.autoscale_view(
            scalex=True,
            scaley=False
        )

        if visible_lines:
            self.rescale_y_axis()

        self.canvas.draw_idle()

    def rescale_y_axis(self):

        visible_y = []

        for ch, line in self.lines.items():

            if not self.channel_vars[ch].get():
                continue

            y_data = line.get_ydata()

            for y in y_data:

                if y != y:   # skip NaN
                    continue

                visible_y.append(y)

        if not visible_y:
            return

        y_min = min(visible_y)

        y_max = max(visible_y)

        if y_min == y_max:

            margin = abs(y_min) * 0.1 if y_min != 0 else 1

            self.ax.set_ylim(
                y_min - margin,
                y_max + margin
            )

        else:

            margin = (y_max - y_min) * 0.1

            self.ax.set_ylim(
                y_min - margin,
                y_max + margin
            )

# --------------------------------------------------------------
# 5️⃣  Main entry point
# --------------------------------------------------------------
def main():
    vcu = MVC_3(port='COM6')   # <-- adjust if you use another COM port
    logger = CSVLogger()
    app = VCUGui(vcu, logger)
    app.mainloop()


if __name__ == '__main__':
    main()

