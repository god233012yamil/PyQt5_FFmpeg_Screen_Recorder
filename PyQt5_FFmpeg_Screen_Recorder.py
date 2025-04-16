import sys
import os
from os import path
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import platform
import signal
import re
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QTime, QRect, QStandardPaths
from PyQt5.QtGui import (
    QPainter, QColor, QFont, QPixmap, QPen, QIcon, QCloseEvent, QMouseEvent,
    QPaintEvent
)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog,
    QMessageBox, QComboBox, QLabel, QSpinBox, QCheckBox, QLineEdit
)
import keyboard  # pip install keyboard


class FFmpegThread(QThread):
    """
    A QThread subclass that runs an FFmpeg subprocess to record video and audio
    in a different thread to avoid freezing the UI thread.
    Emits 'finished' when done or 'error_occurred' if FFmpeg fails.
    Allows clean stopping of FFmpeg using system signals.
    """

    finished = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, cmd: list) -> None:
        """
        Initializes the thread with a list of FFmpeg command arguments.

        Args:
            cmd (list): The full FFmpeg command to execute.
        """
        super().__init__()
        self.cmd = cmd
        self.process = None
        self._stopping = False

    def run(self) -> None:
        """
        Starts the FFmpeg process and monitors for completion or errors.
        Emits appropriate signals based on the result.
        """
        try:
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
            self.process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creation_flags
            )

            _, stderr = self.process.communicate()
            stderr_output = stderr.decode(errors="ignore") if stderr else ""

            if self._stopping:
                self.finished.emit()
            elif self.process.returncode not in (0, 2, 130):
                self.error_occurred.emit(stderr_output.strip())
            else:
                self.finished.emit()

        except Exception as e:
            self.error_occurred.emit(str(e))

    def stop(self) -> None:
        """
        Stops the FFmpeg process by sending the appropriate signal
        based on the current operating system.
        """
        if self.process and self.process.poll() is None:
            try:
                self._stopping = True
                if platform.system() == "Windows":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.process.send_signal(signal.SIGINT)
            except Exception as e:
                self.error_occurred.emit(f"Failed to stop FFmpeg: {e}")


class Logger:
    """
    A Logger class that logs messages to a file using rotating file handler.

    Usage Example:
        if __name__ == "__main__":
        logger = Logger(log_file="logs/my_app.log")
        logger.info("Application started.")
        logger.debug("This is a debug message.")
        logger.warning("Something might be wrong.")
        logger.error("An error occurred.")

    """

    def __init__(self,
                 log_file: str = "app.log", level: int = logging.DEBUG,
                 max_bytes: int = 1_000_000, backup_count: int = 3,
                 log_to_console: bool = True):
        """
        Initialize the logger.

        :param log_file: Path to the log file.
        :param level: Logging level (e.g., logging.DEBUG).
        :param max_bytes: Max file size before rotation (in bytes).
        :param backup_count: Number of backup files to keep.
        :param log_to_console: Also log to console if True.
        """
        self.logger = logging.getLogger("AppLogger")
        self.logger.setLevel(level)

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # Create log directory if it doesn't exist
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

        # File handler with rotation
        file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        # Optional: Console output
        if log_to_console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

    def debug(self, message: str): self.logger.debug(message)
    def info(self, message: str): self.logger.info(message)
    def warning(self, message: str): self.logger.warning(message)
    def error(self, message: str): self.logger.error(message)
    def critical(self, message: str): self.logger.critical(message)


class RecordingOverlay(QWidget):
    """
    A small overlay widget that appears during recording.
    Displays a blinking red "REC" indicator and a recording duration timer.
    """

    def __init__(self) -> None:
        """Initializes the overlay widget, timer, and default state."""
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(180, 50)
        self.move(20, 20)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_overlay)
        self.blink = True
        self.duration = QTime(0, 0, 0)

    def start_blinking(self) -> None:
        """Starts the blinking indicator and timer display."""
        self.blink = True
        self.duration = QTime(0, 0, 0)
        self.timer.start(1000)
        self.show()

    def stop_blinking(self) -> None:
        """Stops the blinking indicator and hides the overlay."""
        self.timer.stop()
        self.hide()

    def update_overlay(self) -> None:
        """Updates the timer each second and triggers repainting of the overlay."""
        self.duration = self.duration.addSecs(1)
        self.update()

    def paintEvent(self, event) -> None:
        """
        Handles the painting of the blinking red REC indicator and the duration text.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self.blink:
            painter.setBrush(QColor(255, 0, 0, 200))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(10, 15, 20, 20)

            painter.setPen(Qt.white)
            painter.setFont(QFont("Arial", 10, QFont.Bold))
            painter.drawText(40, 30, "REC")

        painter.setPen(Qt.white)
        painter.setFont(QFont("Consolas", 10))
        painter.drawText(100, 30, self.duration.toString("hh:mm:ss"))
        self.blink = not self.blink


class RegionSelector(QWidget):
    """
    Transparent full-screen widget that allows the user to select a rectangular screen region.
    Emits a (QRect, QRect) via regionSelected signal when selection is complete.
    """
    # Signal to send the relative and global position of the user chosen region
    regionSelected = pyqtSignal(QRect, QRect)

    def __init__(self, screen_index: int = 1, parent=None) -> None:
        super(RegionSelector, self).__init__(parent)

        self.start_pos = None
        self.end_pos = None
        self.selection = QRect()
        self.start_pos_global = None
        self.end_pos_global = None
        self.selection_global = QRect()

        # Set the flags for this window.
        # The flag "Qt.FramelessWindowHint" is used to make the window frameless and translucent.
        # The flag "Qt.SubWindow" is used to hide this window from appears in the taskbar,
        # or to hide the icon from appears in the taskbar. Indicates that this widget is a sub-window.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SubWindow | Qt.WindowStaysOnTopHint | Qt.Tool)

        # Set window opacity (0.0 = fully transparent, 1.0 = fully opaque)
        self.setWindowOpacity(0.5)

        # Change the background color.
        self.setStyleSheet("background-color: black")

        self.setCursor(Qt.CrossCursor)

        # Set the geometry
        screen_geometry = QApplication.desktop().screenGeometry(screen_index)
        print(f"region screen_geometry: {screen_geometry}")
        self.setGeometry(screen_geometry)

        # Hide this window after creation
        self.hide()

    def start_selection(self) -> None:
        """Displays the widget in full-screen mode to begin region selection."""
        self.showFullScreen()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Captures the start position of the mouse when pressed."""
        # Get the start pos of the region chosen by the user
        # Get the position of the mouse cursor, relative to the widget that received the event
        self.start_pos = event.pos()
        self.end_pos = event.pos()
        # Get the global position of the mouse cursor at the time of the event.
        self.start_pos_global = event.globalPos()
        self.end_pos_global = event.globalPos()
        # Updates the widget
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Updates the end position of the mouse while dragging."""
        # Get the end pos of the region chosen by the user
        self.end_pos = event.pos()
        self.end_pos_global = event.globalPos()
        # Updates the widget
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finalizes the region selection and emits the selected QRect."""
        # Build a relative rectangle with the region chosen by the user
        self.end_pos = event.pos()
        self.selection = QRect(self.start_pos, self.end_pos).normalized()
        # Build a global rectangle with the region chosen by the user
        self.end_pos_global = event.globalPos()
        self.selection_global = QRect(self.start_pos_global, self.end_pos_global).normalized()
        # Emit a signal carrying the relative and global mouse position
        self.regionSelected.emit(self.selection, self.selection_global)
        # Close this widget
        self.close()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.start_pos and self.end_pos:
            painter = QPainter(self)
            # painter.setPen(QPen(QColor(255, 0, 0), 2, Qt.DashLine))
            painter.setPen(QPen(QColor(0, 154, 238), 5, Qt.DashLine))  # color Dark Sky Blue
            painter.setBrush(Qt.NoBrush)  # No fill at all
            painter.drawRect(QRect(self.start_pos, self.end_pos))


class KeyListenerThread(QThread):
    """
        A QThread subclass that listens for global key presses
        using the `keyboard` module and emits a signal with the key name when pressed.

        This allows the PyQt5 GUI to remain responsive while listening for keys
        even when the window is minimized or out of focus.
        """
    key_pressed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.running = True  # Control flag

    # def run(self):
    #     # Only register handlers for specific keys
    #     keyboard.on_press_key("s", lambda e: self.key_pressed.emit("s"))
    #     keyboard.on_press_key("p", lambda e: self.key_pressed.emit("p"))
    #     keyboard.on_press_key("u", lambda e: self.key_pressed.emit("u"))
    #     # Keeps the listener running
    #     keyboard.wait()

    def run(self) -> None:
        """
        Starts listening for the key press in a loop. When the key is detected,
        emits the `key_pressed` signal with the key name.
        """
        while self.running:
            event = keyboard.read_event()
            if event.event_type == keyboard.KEY_DOWN and event.name in ['s', 'p', 'u']:
                self.key_pressed.emit(event.name)

    def stop(self) -> None:
        """ Stops the key listener loop. """
        self.running = False


class ScreenRecorder(QWidget):
    """
    Main application window for screen and audio recording using FFmpeg.
    Provides UI controls for selecting screen, audio input, resolution, frame rate,
    audio bit rate, and optional region-based recording. Also integrates a recording overlay.
    """
    def __init__(self):
        super().__init__()

        self.output_path = None
        self.output_path_button = None
        self.output_path_line_edit = None
        self.ffmpeg_thread = None
        self.stop_button = None
        self.start_button = None
        self.region_checkbox = None
        self.preview = None
        self.bit_rate_combo = None
        self.res_combo = None
        self.fps_spin = None
        self.screen_combo = None
        self.audio_combo = None
        self.capture_rect = None
        self.capture_rect_global = None
        self.selector = None

        # Initialize the UI
        self.initUI()

        # Create an instance of the class Logger to log app messages
        self.logger = Logger(log_file="logs/app.log")
        self.logger.info("Application started.")

        # Load the audio devices
        if self.platform_check():
            self.load_audio_devices()

        # load the screens
        self.load_screens()

        # Create an instance of the class RecordingOverlay to show an overlay widget showing REC
        self.overlay = RecordingOverlay()

        # Create an instance of the class KeyListenerThread to capture pressed keys
        self.key_listener = KeyListenerThread()
        self.key_listener.key_pressed.connect(self.handle_key)

    def initUI(self):

        # Screen Selection
        screen_label = QLabel("Select Screen:")
        self.screen_combo = QComboBox()
        self.screen_combo.setFixedWidth(130)

        # Resolution
        res_label = QLabel("Resolution:")
        self.res_combo = QComboBox()
        self.res_combo.setFixedWidth(130)
        self.res_combo.addItems(["1920x1080", "1280x720", "640x480"])

        # Frame Rate
        fps_label = QLabel("Frame Rate:")
        self.fps_spin = QSpinBox()
        self.fps_spin.setFixedWidth(130)
        self.fps_spin.setRange(10, 60)
        self.fps_spin.setValue(30)

        # Audio Device
        audio_label = QLabel("Audio Input Device:")
        self.audio_combo = QComboBox()
        self.audio_combo.setFixedWidth(130)

        # Audio Bit rate
        bit_rate_label = QLabel("Audio Bit Rate:")
        self.bit_rate_combo = QComboBox()
        self.bit_rate_combo.setFixedWidth(130)
        self.bit_rate_combo.addItems(["96k", "128k", "160k", "192k", "256k", "320k"])

        # Output path label
        output_path_label = QLabel("Output Path:")

        # Output path Line Edit
        self.output_path_line_edit = QLineEdit()
        self.output_path_line_edit.setEnabled(True)
        # Get the default Downloads folder
        downloads_dir = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
        self.output_path_line_edit.setText(f"{downloads_dir}/screen_recording.mp4")

        # Save the current output path
        self.output_path = self.output_path_line_edit.text()

        # Output path button
        self.output_path_button = QPushButton("Save to")
        self.output_path_button.setFixedWidth(100)
        self.output_path_button.clicked.connect(self.save_output_path)
        output_path_button_layout = QHBoxLayout()
        output_path_button_layout.addStretch(1)
        output_path_button_layout.addWidget(self.output_path_button)

        # Preview
        preview_label = QLabel("Screen Preview")
        preview_label.setObjectName("preview_label")
        preview_label.setFont(QFont('Arial', 16, QFont.Bold))
        preview_label_layout = QHBoxLayout()
        preview_label_layout.addStretch(1)
        preview_label_layout.addWidget(preview_label)
        preview_label_layout.addStretch(1)

        # To Preview the Screenshot
        self.preview = QLabel("No Preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(250)
        self.preview.setStyleSheet("""
            background-color: black;    /* Label background color */        
            padding: 5px;               /* Padding inside the label */
            border: 2px solid gray;
            border-radius: 6px;         /* Rounded corners for the label */
        """)

        # To choose the screen region
        self.region_checkbox = QCheckBox("Record Specific Region")

        # Start Button
        self.start_button = QPushButton("Start")
        self.start_button.setFixedWidth(100)

        # Stop Button
        self.stop_button = QPushButton("Stop")
        self.stop_button.setFixedWidth(100)
        self.stop_button.setEnabled(False)

        # Labels vertical layout
        labels_layout = QVBoxLayout()
        labels_layout.addWidget(screen_label)
        labels_layout.addSpacing(5)
        labels_layout.addWidget(res_label)
        labels_layout.addSpacing(5)
        labels_layout.addWidget(fps_label)
        labels_layout.addSpacing(5)
        labels_layout.addWidget(audio_label)
        labels_layout.addSpacing(5)
        labels_layout.addWidget(bit_rate_label)
        labels_layout.addSpacing(5)
        labels_layout.addWidget(output_path_label)
        # labels_layout.addSpacing(5)

        # Widgets vertical layout
        widgets_layout = QVBoxLayout()
        widgets_layout.addWidget(self.screen_combo, 0, Qt.AlignRight)
        widgets_layout.addSpacing(5)
        widgets_layout.addWidget(self.res_combo, 0, Qt.AlignRight)
        widgets_layout.addSpacing(5)
        widgets_layout.addWidget(self.fps_spin, 0, Qt.AlignRight)
        widgets_layout.addSpacing(5)
        widgets_layout.addWidget(self.audio_combo, 0, Qt.AlignRight)
        widgets_layout.addSpacing(5)
        widgets_layout.addWidget(self.bit_rate_combo, 0, Qt.AlignRight)
        widgets_layout.addSpacing(5)
        widgets_layout.addWidget(self.output_path_line_edit)
        # widgets_layout.addSpacing(5)

        # Labels and Widgets horizontal layout
        labels_widgets_layout = QHBoxLayout()
        labels_widgets_layout.addLayout(labels_layout)
        labels_widgets_layout.addLayout(widgets_layout)

        # Buttons horizontal layout
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.start_button)
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(self.stop_button)

        # Main Layout Setup
        layout = QVBoxLayout()
        layout.addLayout(labels_widgets_layout)
        # layout.addSpacing(5)
        layout.addLayout(output_path_button_layout)
        # layout.addSpacing(5)
        layout.addLayout(preview_label_layout)
        layout.addWidget(self.preview)
        layout.addSpacing(5)
        layout.addWidget(self.region_checkbox)
        layout.addSpacing(5)
        layout.addLayout(buttons_layout)
        layout.addStretch(1)

        # Event Connections
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button.clicked.connect(self.stop_recording)
        self.screen_combo.currentIndexChanged.connect(self.update_preview)
        self.region_checkbox.stateChanged.connect(self.select_region)

        # Main window setup
        self.setWindowTitle("Screen Recorder")
        self.setFixedSize(500, 600)
        self.setLayout(layout)

        # Set an icon for this window.
        file_name = os.path.dirname(os.path.realpath(__file__)) + "\\Images\\screen_recorder_icon.png"
        if path.exists(file_name):
            self.setWindowIcon(QIcon(file_name))
        else:
            self.logger.warning("Application icon not found.")

    def enable_widgets(self, enable: bool) -> None:
        for child in self.findChildren(QWidget):
            if enable:
                child.setEnabled(True)
            else:
                child.setDisabled(True)

    def platform_check(self):
        """Check if the required dependencies are available"""
        try:
            # Check if ffmpeg is installed
            process = subprocess.Popen(['ffmpeg', '-version'],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            stdout, _ = process.communicate()

            if process.returncode != 0:
                self.logger.error("ffmpeg is not installed or not found in PATH.\n"
                                  "Please install ffmpeg before using this application.")
                QMessageBox.critical(self, "Error",
                                     "ffmpeg is not installed or not found in PATH.\n"
                                     "Please install ffmpeg before using this application.")
                return False

            line = stdout.decode().split('\n')[0]
            self.logger.info(f"Found ffmpeg: {line.rstrip()}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to check dependencies: {str(e)}\n"
                              "Please make sure ffmpeg is installed.")
            QMessageBox.critical(self, "Error",
                                 f"Failed to check dependencies: {str(e)}\n"
                                 "Please make sure ffmpeg is installed.")
            return False

    def select_region(self):
        """Activates the region selector overlay when the checkbox is checked."""
        if self.region_checkbox.isChecked():
            # Get the current screen
            screen_index = self.screen_combo.currentIndex()
            self.logger.debug(f"Screen index: {screen_index}")
            # Create an instance of the class RegionSelector
            self.selector = RegionSelector(screen_index)
            self.selector.regionSelected.connect(self.set_region)
            # Update UI
            self.showMinimized()
            self.enable_widgets(False)  # disable all widgets
            self.stop_button.setEnabled(True)
            # Called start_selection after a elapsed time
            QTimer.singleShot(100, self.selector.start_selection)

    # def set_region(self, rect):
    def set_region(self, rect: QRect, global_rect: QRect):
        """Stores the selected QRect(s) from the RegionSelector widget.

        Args:
            rect (QRect): The relative screen region to record.
            global_rect (QRect): The global screen region to record.
        """
        self.logger.debug(f"selected relative screen region: {rect}")
        self.logger.debug(f"selected global screen region: {global_rect}")
        self.capture_rect = rect
        self.capture_rect_global = global_rect
        self.update_preview()
        # Update UI
        if self.isMinimized() or self.isHidden():
            self.showNormal()
            self.raise_()
            self.activateWindow()  # Ensure it comes to the foreground
        self.enable_widgets(True)
        self.region_checkbox.setChecked(False)

    def load_audio_devices(self):
        """Fetches and populates the list of available audio input devices using FFmpeg."""
        try:
            result = subprocess.run(
                ['ffmpeg', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True
            )
            audio_devices = re.findall(r'"(.+?)" \(audio\)', result.stderr)
            self.audio_combo.addItem("None")
            if audio_devices:
                self.audio_combo.addItems(audio_devices)
            else:
                self.logger.warning(f"No audio devices found")
        except Exception as e:
            self.audio_combo.addItem("None")
            self.logger.error(f"Error finding audio devices {str(e)}")

    def load_screens(self):
        """Populates the screen combo box with all connected screens."""
        screen_count = QApplication.desktop().screenCount()
        for i in range(screen_count):
            self.screen_combo.addItem(f"Screen {i + 1}")
        self.update_preview()

    def update_preview(self):
        """ Show a preview of the screen or the screen area to be recorded """
        # Get the current screen
        index = self.screen_combo.currentIndex()
        screens = QApplication.screens()
        if 0 <= index < len(screens):
            # Select the current screen
            screen = screens[index]
            #
            if self.capture_rect is None:
                screenshot = screen.grabWindow(0)
            else:
                screenshot = screen.grabWindow(0, self.capture_rect.x(), self.capture_rect.y(),
                                               self.capture_rect.width(), self.capture_rect.height())
            #
            scaled = screenshot.scaled(
                self.preview.width(), self.preview.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            #
            self.preview.setPixmap(QPixmap(scaled))
            # Clear
            self.capture_rect = None

    def save_output_path(self) -> None:
        """ Show a File dialog to allow the user to choose the directory where to save the recording file """
        output_path, _ = QFileDialog.getSaveFileName(self,
                                                     "Save Video",
                                                     "screen_record",
                                                     "MP4 files (*.mp4)")
        if output_path:
            self.output_path = output_path
            self.output_path_line_edit.setText(self.output_path)
        else:
            QMessageBox.critical(self, "Error", "No file chosen to save the screen recording")
            self.logger.error("No file chosen to save the screen recording")

    def handle_key(self, key: str) -> None:
        print(f"Key pressed: {key}")
        if key == "s":
            self.stop_recording()
            self.logger.debug("The key \"s\" was pressed to stop recording")

    def start_recording(self):
        """Begins screen and audio recording by launching the FFmpeg thread with selected settings."""
        if not self.output_path:
            return
        screen_index = self.screen_combo.currentIndex()
        screen_geometry = QApplication.desktop().screenGeometry(screen_index)
        self.logger.debug(f"Recording screen geometry: {screen_geometry}")
        offset_x, offset_y = screen_geometry.x(), screen_geometry.y()
        resolution = self.res_combo.currentText()

        audio_device = self.audio_combo.currentText()
        fps = str(self.fps_spin.value())
        bit_rate = self.bit_rate_combo.currentText()

        if self.capture_rect_global:
            offset_x = self.capture_rect_global.x()
            self.logger.debug(f"Recording screen with x offset: {offset_x}")
            offset_y = self.capture_rect_global.y()
            self.logger.debug(f"Recording screen with y offset: {offset_y}")
            # When we choose a screen region, the width or the height might not be an even number,
            # and the height and width must be divisible by 2.
            # If either the width or the height are not divisible by 2, the ffmpeg command will fail.
            # Check if the width is divisible by 2, if not add 1 to make it an even number.
            width = self.capture_rect_global.width() if self.capture_rect_global.width() % 2 == 0 \
                else self.capture_rect_global.width() + 1
            # Check if the height is divisible by 2, if not add 1 to make it an even number.
            height = self.capture_rect_global.height() if self.capture_rect_global.height() % 2 == 0 \
                else self.capture_rect_global.height() + 1
            resolution = f"{width}x{height}"
            self.logger.debug(f"Recording screen with resolution: {resolution}")
            self.capture_rect_global = None

        # Build the ffmpeg command
        cmd = ['ffmpeg']

        # Input options for screen capture
        cmd.extend([
            "-f", "gdigrab",
            "-framerate", fps,
            "-offset_x", str(offset_x),
            "-offset_y", str(offset_y),
            "-video_size", resolution,
            "-i", "desktop",
        ])

        # Add audio capture if enabled
        if audio_device != "None":
            cmd.extend([
                "-f", "dshow",
                "-i", f"audio={audio_device}",
            ])

        # Output options
        cmd.extend([
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
        ])

        # Add audio codec if we're capturing audio
        if audio_device != "None":
            cmd.extend([
                "-acodec", "aac",
                "-b:a", bit_rate,
            ])

        # Add output file (overwrite if exists)
        cmd.extend(['-y', self.output_path])

        # Create a thread to record the screen in another thread
        self.ffmpeg_thread = FFmpegThread(cmd)
        self.ffmpeg_thread.finished.connect(self.recording_finished)
        self.ffmpeg_thread.error_occurred.connect(self.recording_failed)
        self.ffmpeg_thread.start()

        # Update the UI
        self.overlay.start_blinking()
        self.enable_widgets(False)
        self.stop_button.setEnabled(True)
        self.showMinimized()

        # Start listening for key press after minimizing the windows
        self.key_listener.start()

    def stop_recording(self):
        """Stops the recording process gracefully by signaling the FFmpeg thread to stop."""
        if self.ffmpeg_thread:
            self.ffmpeg_thread.stop()
        if self.selector:  # new
            self.selector.close()

    def recording_finished(self):
        """Handles UI changes after recording has successfully stopped."""
        self.overlay.stop_blinking()
        self.enable_widgets(True)
        self.stop_button.setEnabled(False)
        if self.isMinimized() or self.isHidden():
            self.showNormal()
            self.raise_()
            self.activateWindow()  # Ensure it comes to the foreground
        QMessageBox.information(self, "Done", "Recording stopped and saved.")
        self.logger.info("Recording stopped and saved.")

    def recording_failed(self, error_msg):
        """Displays an error message when FFmpeg encounters a failure.

        Args:
            error_msg (str): The error message to display.
        """
        self.overlay.stop_blinking()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.logger.error(f"Recording failed:\n{error_msg}")
        QMessageBox.critical(self, "Error", f"Recording failed:\n{error_msg}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.ffmpeg_thread and self.ffmpeg_thread.isRunning():
            self.ffmpeg_thread.stop()
            event.ignore()
            return
        else:
            if self.key_listener and self.key_listener.isRunning():
                self.key_listener.stop()
        self.logger.info("Application finished.")
        event.accept()


def main():
    app = QApplication(sys.argv)
    # Change the font used by the app
    app.setFont(QFont('Arial', 10, QFont.Normal))
    window = ScreenRecorder()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
