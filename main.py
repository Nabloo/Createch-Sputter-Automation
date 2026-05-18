import serial
import time
import csv
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
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
    """
    Main GUI application for sputter system monitoring.

    Handles:
    - Live VCU acquisition
    - Log file playback
    - Multi-panel plotting
    - Device connection management
    """

    UPDATE_MS = 1000

    def __init__(self, vcu: MVC_3, logger: CSVLogger):
        super().__init__()

        self._init_state(vcu, logger)
        self._build_widgets()

    # ------------------------------------------------------
    def _init_state(self, vcu, logger):
        """
        Initialize application state variables.
        """
        self.title("Createch Sputter readout")
        self.geometry("1400x700")

        self.vcu = vcu
        self.logger = logger

        self.running = False
        self.live_mode = True

        self.unit = "unknown"
        self.start_time = None

        self.time_data = []
        self.press_data = {1: [], 2: [], 3: []}

        self.log_time = []
        self.log_data = {1: [], 2: [], 3: []}
        self.log_start_datetime = None
        self.log_end_datetime = None

        self.panels = []
    # ------------------------------------------------------
    def _build_widgets(self):
        """
        Create all GUI widgets and layout.
        """
        self._build_top_bar()
        self._build_log_range_slider()
        self._build_connection_bar()
        self._build_panel_area()
        self._build_readout()
        self.add_panel()

    def _build_top_bar(self):
        """
        Start/Stop + log controls.
        """
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

        ttk.Button(
            top_bar,
            text="Load Log",
            command=self.load_log_file
        ).pack(side="right", padx=5)

        ttk.Button(
            top_bar,
            text="Live Mode",
            command=self.switch_to_live_mode
        ).pack(side="right", padx=5)

        # --------------------------------------------------
        # LOG TIME RANGE CONTROLS (ADD HERE)
        # --------------------------------------------------
        range_frame = ttk.Frame(top_bar)
        range_frame.pack(side="right", padx=10)

        ttk.Label(range_frame, text="From (hh:mm):").pack(side="left")

        self.log_from_var = tk.StringVar()
        self.log_from_entry = ttk.Entry(range_frame, textvariable=self.log_from_var, width=7)
        self.log_from_entry.pack(side="left", padx=2)

        ttk.Label(range_frame, text="To (hh:mm):").pack(side="left")

        self.log_to_var = tk.StringVar()
        self.log_to_entry = ttk.Entry(range_frame, textvariable=self.log_to_var, width=7)
        self.log_to_entry.pack(side="left", padx=2)

        ttk.Button(
            range_frame,
            text="Apply",
            command=self.apply_log_time_filter
        ).pack(side="left", padx=5)

    def _build_log_range_slider(self):
        """
        Creates start/end time sliders for log playback filtering.

        The sliders are only active when a log file is loaded and
        are clamped to the recorded dataset time range.
        """

        self.log_slider_frame = ttk.LabelFrame(self, text="Log Time Range")
        self.log_slider_frame.pack(fill="x", padx=10, pady=5)

        self.log_range_label = ttk.Label(self.log_slider_frame, text="No log loaded")
        self.log_range_label.pack(side="top", anchor="w", padx=5)

        slider_row = ttk.Frame(self.log_slider_frame)
        slider_row.pack(fill="x", padx=5, pady=5)

        # START SLIDER
        self.slider_start = ttk.Scale(
            slider_row,
            from_=0,
            to=1,
            orient="horizontal",
            command=self._on_slider_change
        )
        self.slider_start.pack(side="left", fill="x", expand=True, padx=5)

        # END SLIDER
        self.slider_end = ttk.Scale(
            slider_row,
            from_=0,
            to=1,
            orient="horizontal",
            command=self._on_slider_change
        )
        self.slider_end.pack(side="left", fill="x", expand=True, padx=5)

        self.slider_start.set(0)
        self.slider_end.set(1)

    # ------------------------------------------------------
    def _build_panel_area(self):
        """
        Multi-panel container setup.
        """
        self.panel_container = ttk.Frame(self)
        self.panel_container.pack(fill="both", expand=True, padx=10, pady=5)

    # ------------------------------------------------------
    def _build_connection_bar(self):
        """
        COM + baudrate selection UI.
        """
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

        ttk.Button(
            conn_frame,
            text="⟳",
            width=3,
            command=self.refresh_ports
        ).pack(side="left", padx=5)

        ttk.Label(conn_frame, text="Baudrate:").pack(side="left", padx=10)

        self.baud_var = tk.StringVar(value=str(self.vcu.baudrate))

        ttk.Entry(
            conn_frame,
            textvariable=self.baud_var,
            width=10
        ).pack(side="left", padx=5)

        self._init_default_port()

    # ------------------------------------------------------
    def _build_readout(self):
        """
        Current pressure readout labels.
        """
        frame = ttk.LabelFrame(self, text="Current Readings")
        frame.pack(fill="x", padx=10, pady=5)

        self.var_ch = {}

        for ch in (1, 2, 3):
            var = tk.StringVar(value=f"CH{ch}: – –")

            ttk.Label(
                frame,
                textvariable=var,
                width=30,
                anchor="w"
            ).pack(side="left", padx=5)

            self.var_ch[ch] = var

    # ------------------------------------------------------
    def _init_default_port(self):
        """
        Select COM6 if available, otherwise fallback.
        """
        ports = self.get_available_ports()

        self.com_dropdown["values"] = ports

        if "COM6" in ports:
            self.com_var.set("COM6")
        elif ports:
            self.com_var.set(ports[0])

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

    def load_log_file(self):
        """
        Load a CSV log file and switch GUI into offline playback mode.

        This function:
        - Reads timestamped pressure logs
        - Stores both datetime and relative time
        - Computes full available time range
        - Updates GUI range display
        """

        filepath = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv")]
        )

        if not filepath:
            return

        self.live_mode = False

        self.log_time = []
        self.log_datetime = []
        self.log_data = {1: [], 2: [], 3: []}

        with open(filepath, "r") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if len(rows) < 2:
            messagebox.showerror("Error", "File is empty or invalid")
            return

        header = rows[0]

        ch_cols = {}
        time_idx = 0

        for i, col in enumerate(header):
            if col.startswith("ch") and "_pressure" in col:
                ch = int(col[2])
                ch_cols[ch] = i
            elif col == "timestamp":
                time_idx = i

        from datetime import datetime

        base_time = None

        for row in rows[1:]:
            try:
                t = datetime.strptime(row[time_idx], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            if base_time is None:
                base_time = t

            self.log_datetime.append(t)
            self.log_time.append((t - base_time).total_seconds())

            for ch in ch_cols:
                try:
                    val = float(row[ch_cols[ch]])
                except Exception:
                    val = float("nan")

                self.log_data[ch].append(val)

        # -----------------------------
        # STORE RANGE INFO
        # -----------------------------
        self.log_start_time = self.log_time[0]
        self.log_end_time = self.log_time[-1]

        # update slider bounds
        self.slider_start.config(from_=self.log_start_time, to=self.log_end_time)
        self.slider_end.config(from_=self.log_start_time, to=self.log_end_time)

        self.slider_start.set(self.log_start_time)
        self.slider_end.set(self.log_end_time)

        # update label
        start_hhmm = self._format_hhmm(self.log_time[0])
        end_hhmm = self._format_hhmm(self.log_time[-1])

        duration_sec = self.log_time[-1] - self.log_time[0]
        duration_h = int(duration_sec // 3600)
        duration_m = int((duration_sec % 3600) // 60)

        self.log_range_label.config(
            text=(
                f"Available range: {start_hhmm} → {end_hhmm}  "
                f"(duration {duration_h:02d}:{duration_m:02d})"
            )
        )

        self.update_log_plot()

    def apply_log_time_filter(self):
        """
        Filter loaded log data by user-defined HH:MM time range.
        """

        if not self.log_datetime:
            return

        try:
            t_from = datetime.strptime(self.log_from_var.get(), "%H:%M").time()
            t_to = datetime.strptime(self.log_to_var.get(), "%H:%M").time()
        except ValueError:
            messagebox.showerror(
                "Format error",
                "Please use HH:MM format (e.g. 12:30)"
            )
            return

        filtered_time = []
        filtered_data = {1: [], 2: [], 3: []}

        for i, dt in enumerate(self.log_datetime):

            current_time = dt.time()

            if t_from <= t_to:
                valid = t_from <= current_time <= t_to
            else:
                # handles overnight ranges like 23:00 → 02:00
                valid = current_time >= t_from or current_time <= t_to

            if valid:
                filtered_time.append(self.log_time[i])

                for ch in filtered_data:
                    filtered_data[ch].append(self.log_data[ch][ch] if False else self.log_data[ch][i])

        if not filtered_time:
            messagebox.showwarning("No data", "No data in selected time range")
            return

        for panel in self.panels:
            panel.update_plot(filtered_time, filtered_data, self.unit)


    def update_log_plot(self):
        """
        Render loaded log file data across all active panels.

        This function is called after a CSV log is loaded and is responsible
        for pushing historical time-series data into all DevicePanel instances.

        Returns
        -------
        None
        """
        for panel in self.panels:
            panel.update_plot(
                self.log_time,
                self.log_data,
                self.unit
            )
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
        """
        Acquire data from VCU and update plots.
        """

        if not self.running:
            return

        readings = self.vcu.read_all_channels()
        self.logger.write(readings, self.unit)

        # update readout labels
        for r in readings:
            ch = r["channel"]

            val = (
                f"{r['pressure']:.3g}"
                if r["pressure"] is not None
                else "—"
            )

            self.var_ch[ch].set(
                f"CH{ch}: {val} {self.unit} | {r['status']}"
            )

        elapsed = time.time() - self.start_time

        # -----------------------
        # LIVE MODE ONLY
        # -----------------------
        if self.live_mode:
            self.time_data.append(elapsed)

            for r in readings:
                ch = r["channel"]
                self.press_data[ch].append(
                    r["pressure"] if r["pressure"] is not None else float("nan")
                )

            self._update_all_panels(self.time_data, self.press_data)

        self.after(self.UPDATE_MS, self._acquire)

    def _update_all_panels(self, t, data):
        """
        Push data to all panels.
        """
        for panel in self.panels:
            panel.update_plot(t, data, self.unit)

    def _on_slider_change(self, _event=None):
        """
        Called whenever the user moves start/end slider.

        Filters displayed log data to selected time range.
        """

        if not self.log_time:
            return

        t_min = min(self.slider_start.get(), self.slider_end.get())
        t_max = max(self.slider_start.get(), self.slider_end.get())

        filtered_t = []
        filtered_data = {1: [], 2: [], 3: []}

        for i, t in enumerate(self.log_time):
            if t_min <= t <= t_max:
                filtered_t.append(t)
                for ch in filtered_data:
                    filtered_data[ch].append(self.log_data[ch][i])

        # update all panels
        for panel in self.panels:
            panel.update_plot(filtered_t, filtered_data, self.unit)

    def switch_to_live_mode(self):
        """
        Switch GUI back to live acquisition mode.

        This clears previously loaded log data buffers and re-enables
        real-time plotting from the connected VCU device.

        Returns
        -------
        None
        """
        self.live_mode = True

        self.time_data = []
        self.press_data = {1: [], 2: [], 3: []}

        messagebox.showinfo("Mode", "Switched to live mode")

    def _format_hhmm(self, seconds):
        """
        Convert seconds (float) into HH:MM string.

        Parameters
        ----------
        seconds : float
            Time in seconds.

        Returns
        -------
        str
            Formatted time string HH:MM
        """
        total_minutes = int(seconds // 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours:02d}:{minutes:02d}"

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

