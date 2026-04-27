import logging
import time
import threading
import ssl
import socket
from ftplib import FTP_TLS
from database import SessionLocal, Printer, PrinterStatus, Job, JobStatus

from bambu_connect.WatchClient import WatchClient
from bambu_connect.ExecuteClient import ExecuteClient

logger = logging.getLogger(__name__)


class ImplicitFTPS(FTP_TLS):
    """
    FTP_TLS subclass that supports implicit FTPS (port 990).

    Standard FTP_TLS.connect() opens a plain TCP socket and later upgrades
    via AUTH TLS (explicit FTPS).  Bambu Lab printers expect TLS to be
    established *immediately* upon connection (implicit FTPS), so we
    override connect() to wrap the socket in TLS before reading the
    server banner.

    Also works around a known Python FTP_TLS deadlock: when closing
    a TLS data connection, ssl.SSLSocket.close() sends a TLS close_notify
    and waits for the peer's close_notify, but the FTP server won't send
    it until after it writes the 226 response on the control channel —
    creating a deadlock that results in a read timeout.
    """

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        # 1. Plain TCP connection
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout
        )
        self.af = self.sock.family

        # 2. Immediately wrap in TLS (implicit FTPS)
        self.sock = self.context.wrap_socket(
            self.sock, server_hostname=self.host
        )

        # 3. Create text-mode file handle for FTP protocol lines
        self.file = self.sock.makefile("r", encoding=self.encoding)

        # 4. Read and store the server welcome banner (e.g. "220 ...")
        self.welcome = self.getresp()
        return self.welcome

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """
        Upload a file in binary mode — with TLS-safe data channel close.

        Works around a Python FTP_TLS deadlock: the standard close()
        on an SSLSocket sends a TLS close_notify and blocks waiting
        for the peer's close_notify, but the FTP server won't send it
        until after it writes the 226 response on the control channel.
        """
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            # Close the data connection carefully to avoid both:
            # - the TLS close_notify deadlock (see docstring above)
            # - file truncation from a premature RST
            if isinstance(conn, ssl.SSLSocket):
                # Give the TLS shutdown enough time to flush remaining
                # data and send close_notify, but don't block forever.
                conn.settimeout(10)
                try:
                    # unwrap() sends close_notify and returns the raw socket;
                    # this lets the peer know we're done without an abrupt RST.
                    raw = conn.unwrap()
                    raw.close()
                except (ssl.SSLError, OSError, TimeoutError):
                    # TLS shutdown failed — force-close.
                    try:
                        conn.close()
                    except OSError:
                        pass
            else:
                conn.close()
        # Now read the server's 226 "Transfer complete" on the control channel
        return self.voidresp()


class BambuClient:
    """
    Adapter around bambu-connect WatchClient + ExecuteClient.
    Replaces the previous manual paho-mqtt implementation.
    """

    # Serial prefix → model name mapping
    SERIAL_MODEL_MAP = {
        "00M": "X1 Carbon",
        "00W": "X1 Carbon",
        "01P": "X1 Carbon",
        "01S": "X1",
        "030": "P1P",
        "03W": "A1",
        "039": "A1 Mini",
        "01J": "P1S",
    }

    def __init__(self, printer_id: int):
        self.printer_id = printer_id
        self._watch_client: WatchClient | None = None
        self._exec_client: ExecuteClient | None = None
        self._lock = threading.Lock()
        self._hw_info_saved = False  # only persist once per session

        # Live print data (updated every MQTT push, ~5 s)
        self.print_info: dict = {
            "remaining_time_min": None,   # minutes remaining
            "progress": None,             # 0-100
            "gcode_state": None,          # IDLE / RUNNING / FINISH / FAILED / PAUSE
            "subtask_name": None,         # current file being printed
            "nozzle_temper": None,
            "nozzle_target_temper": None,
            "bed_temper": None,
            "bed_target_temper": None,
            "chamber_temper": None,
            "layer_num": None,
            "total_layer_num": None,
            "fan_speed": None,            # cooling fan %
            "wifi_signal": None,
            "print_error": None,
            "stg_cur": None,              # current print stage integer
            "last_update": None,          # epoch timestamp
        }

        # Extended hardware info (volatile, from MQTT)
        self.hw_info: dict = {
            "model": None,
            "firmware_version": None,
            "ams_installed": False,
            "ams_trays": [],              # list of filament tray info
            "nozzle_diameter": None,
            "has_camera": False,
            "wifi_signal": None,
            "lifecycle": None,
            "sdcard": None,
            "fan_gear": None,
            "speed_level": None,
            "lights": [],
            "heatbreak_fan_speed": None,
            "big_fan1_speed": None,
            "big_fan2_speed": None,
        }

        db = SessionLocal()
        self.printer = db.query(Printer).filter(Printer.id == printer_id).first()
        db.close()

        if not self.printer:
            raise ValueError(f"Printer {printer_id} not found in DB")

        # Detect model from serial prefix
        self.hw_info["model"] = self._detect_model(self.printer.serial)

    @classmethod
    def _detect_model(cls, serial: str) -> str:
        """Infer printer model from serial number prefix."""
        if serial and len(serial) >= 3:
            prefix = serial[:3].upper()
            return cls.SERIAL_MODEL_MAP.get(prefix, f"Unknown ({prefix})")
        return "Unknown"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        logger.info(
            f"Connecting to Bambu Printer {self.printer.serial} at {self.printer.ip}"
        )
        try:
            self._watch_client = WatchClient(
                hostname=self.printer.ip,
                access_code=self.printer.access_code,
                serial=self.printer.serial,
            )
            self._watch_client.start(
                message_callback=self._on_status_update,
                on_connect_callback=self._on_connected,
            )
        except Exception as e:
            logger.error(
                f"Failed to connect to printer {self.printer.serial}: {e}"
            )
            self.update_status(PrinterStatus.OFFLINE)

    def disconnect(self):
        if self._watch_client:
            try:
                self._watch_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WatchClient: {e}")
            self._watch_client = None

    # ------------------------------------------------------------------
    # WatchClient callbacks
    # ------------------------------------------------------------------

    def _on_connected(self):
        logger.info(f"WatchClient connected to printer {self.printer.serial}")
        # Request a full status dump so we don't wait for next MQTT push
        try:
            exec_client = self._get_exec_client()
            exec_client.dump_info()
            exec_client.disconnect()
        except Exception as e:
            logger.warning(f"Could not dump_info on connect: {e}")
        self.update_status(PrinterStatus.READY)

    def _on_status_update(self, status):
        """
        Called by WatchClient for every MQTT message.
        `status` is a bambu_connect.utils.models.PrinterStatus instance.
        """
        try:
            if status.gcode_state:
                self._handle_gcode_state(status.gcode_state)

            if status.mc_percent is not None:
                self._update_progress(status.mc_percent)

            # ── Update volatile print_info dict ─────────────────────
            self._update_print_info(status)

            # ── Update hardware info ────────────────────────────────
            self._update_hardware_info(status)

        except Exception as e:
            logger.error(f"Error processing status update: {e}")

    def _update_print_info(self, status):
        """Merge incoming MQTT fields into the in-memory print_info dict."""
        info = self.print_info
        if status.mc_remaining_time is not None:
            info["remaining_time_min"] = status.mc_remaining_time
        if status.mc_percent is not None:
            info["progress"] = status.mc_percent
        if status.gcode_state is not None:
            info["gcode_state"] = status.gcode_state
        if status.subtask_name is not None:
            info["subtask_name"] = status.subtask_name
        if status.nozzle_temper is not None:
            info["nozzle_temper"] = status.nozzle_temper
        if status.nozzle_target_temper is not None:
            info["nozzle_target_temper"] = status.nozzle_target_temper
        if status.bed_temper is not None:
            info["bed_temper"] = status.bed_temper
        if status.bed_target_temper is not None:
            info["bed_target_temper"] = status.bed_target_temper
        if status.chamber_temper is not None:
            info["chamber_temper"] = status.chamber_temper
        if status.layer_num is not None:
            info["layer_num"] = status.layer_num
        if status.total_layer_num is not None:
            info["total_layer_num"] = status.total_layer_num
        if status.cooling_fan_speed is not None:
            info["fan_speed"] = status.cooling_fan_speed
        if status.wifi_signal is not None:
            info["wifi_signal"] = status.wifi_signal
        if status.print_error is not None:
            info["print_error"] = status.print_error
        if status.stg_cur is not None:
            info["stg_cur"] = status.stg_cur
        info["last_update"] = time.time()

    def _update_hardware_info(self, status):
        """Collect hardware/config data from MQTT status into hw_info dict."""
        hw = self.hw_info

        # Firmware version from online.version
        if status.online and status.online.version is not None:
            hw["firmware_version"] = str(status.online.version)

        # AMS info
        if status.ams and status.ams.ams:
            hw["ams_installed"] = True
            trays = []
            for ams_entry in status.ams.ams:
                if ams_entry.tray:
                    for tray in ams_entry.tray:
                        if tray.tray_type:
                            trays.append({
                                "slot": tray.id,
                                "type": tray.tray_type,
                                "color": tray.tray_color,
                                "name": tray.tray_sub_brands or tray.tray_type,
                                "remain": tray.remain,
                                "temp": tray.tray_temp,
                            })
            if trays:
                hw["ams_trays"] = trays
        elif status.ams and status.ams.ams_exist_bits == "0":
            hw["ams_installed"] = False

        # Camera presence
        if status.ipcam and status.ipcam.ipcam_dev:
            hw["has_camera"] = True

        # WiFi signal
        if status.wifi_signal is not None:
            hw["wifi_signal"] = status.wifi_signal

        # Misc hardware fields
        if status.lifecycle is not None:
            hw["lifecycle"] = status.lifecycle
        if status.sdcard is not None:
            hw["sdcard"] = status.sdcard
        if status.fan_gear is not None:
            hw["fan_gear"] = status.fan_gear
        if status.spd_lvl is not None:
            hw["speed_level"] = status.spd_lvl
        if status.heatbreak_fan_speed is not None:
            hw["heatbreak_fan_speed"] = status.heatbreak_fan_speed
        if status.big_fan1_speed is not None:
            hw["big_fan1_speed"] = status.big_fan1_speed
        if status.big_fan2_speed is not None:
            hw["big_fan2_speed"] = status.big_fan2_speed
        if status.lights_report:
            hw["lights"] = [
                {"node": lr.node, "mode": lr.mode}
                for lr in status.lights_report
            ]

        # Persist key fields to DB once (after first full dump)
        if not self._hw_info_saved and hw.get("firmware_version"):
            self._persist_hw_to_db()
            self._hw_info_saved = True

    def _persist_hw_to_db(self):
        """Save detected model and firmware version to the database."""
        db = SessionLocal()
        try:
            printer = db.query(Printer).filter(
                Printer.id == self.printer_id
            ).first()
            if printer:
                changed = False
                model = self.hw_info.get("model")
                if model and printer.model != model:
                    printer.model = model
                    changed = True
                fw = self.hw_info.get("firmware_version")
                if fw and printer.firmware_version != fw:
                    printer.firmware_version = fw
                    changed = True
                ams = self.hw_info.get("ams_installed", False)
                if printer.ams_installed != ams:
                    printer.ams_installed = ams
                    changed = True
                if changed:
                    db.commit()
                    logger.info(
                        f"Saved hardware info for {self.printer.serial}: "
                        f"model={model}, firmware={fw}, ams={ams}"
                    )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _handle_gcode_state(self, state: str):
        """
        Bambu gcode_state values:
            IDLE     – printer is idle (no job running)
            PREPARE  – preparing to print (heating, leveling, etc.)
            RUNNING  – actively printing
            PAUSE    – print paused by user or error
            FINISH   – print completed successfully
            FAILED   – print failed

        State machine rules:
            - Only FINISH triggers job completion (→ WAITING_CLEAN).
            - Only FAILED triggers job failure (→ ERROR).
            - IDLE while PRINTING is IGNORED — this is a race condition
              that occurs between sending the print command and the
              printer actually starting (IDLE → PREPARE → RUNNING).
            - IDLE transitions to READY only if there is no active job
              (i.e., after WAITING_CLEAN has been acknowledged).
            - PREPARE / RUNNING / PAUSE all keep the printer in PRINTING.
        """
        db = SessionLocal()
        try:
            printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
            if not printer:
                return

            current_status = printer.status
            new_status = current_status

            if state == "IDLE":
                # Only transition to READY if there is NO active job.
                # If we're PRINTING and get IDLE, it's the race-condition
                # window — ignore it so we don't prematurely complete.
                if current_status == PrinterStatus.PRINTING:
                    if printer.current_job_id is None:
                        # No job attached — safe to go idle
                        new_status = PrinterStatus.READY
                    else:
                        # Job still assigned → wait for FINISH / FAILED
                        logger.debug(
                            f"Ignoring IDLE during PRINTING for "
                            f"printer {self.printer.serial} "
                            f"(job {printer.current_job_id} still assigned)"
                        )
                elif current_status == PrinterStatus.WAITING_CLEAN:
                    pass  # stay in WAITING_CLEAN until button press
                elif current_status == PrinterStatus.FINISHED:
                    new_status = PrinterStatus.WAITING_CLEAN
                elif current_status == PrinterStatus.ERROR:
                    pass  # stay in ERROR
                else:
                    # OFFLINE or READY — just mark READY
                    new_status = PrinterStatus.READY

            elif state in ("RUNNING", "PREPARE", "PAUSE"):
                new_status = PrinterStatus.PRINTING

            elif state == "FINISH":
                # If we manually set READY from the UI, ignore the persistent FINISH broadcast
                if current_status not in (PrinterStatus.READY, PrinterStatus.OFFLINE):
                    new_status = PrinterStatus.WAITING_CLEAN
                    self._complete_current_job(db, printer)

            elif state == "FAILED":
                new_status = PrinterStatus.ERROR
                self._fail_current_job(db, printer)

            if new_status != current_status:
                logger.info(
                    f"Printer {self.printer.serial} state: "
                    f"{current_status} → {new_status} "
                    f"(gcode_state={state})"
                )
                printer.status = new_status
                db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def update_status(self, status: PrinterStatus):
        db = SessionLocal()
        try:
            printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
            if printer and printer.status != status:
                # Don't overwrite WAITING_CLEAN with READY on reconnect
                if not (
                    printer.status == PrinterStatus.WAITING_CLEAN
                    and status == PrinterStatus.READY
                ):
                    printer.status = status
                    db.commit()
        finally:
            db.close()

    def _update_progress(self, progress: int):
        db = SessionLocal()
        try:
            printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
            if printer and printer.current_job_id:
                job = db.query(Job).filter(Job.id == printer.current_job_id).first()
                if job:
                    if job.progress != progress:
                        job.progress = progress
                        db.commit()
                    # Auto-complete when progress reaches 100%
                    if progress >= 100 and job.status == JobStatus.PRINTING:
                        logger.info(
                            f"Job {job.id} reached 100% — marking COMPLETED"
                        )
                        self._complete_current_job(db, printer)
                        printer.status = PrinterStatus.WAITING_CLEAN
                        db.commit()
        finally:
            db.close()

    def _complete_current_job(self, db, printer):
        if printer.current_job_id:
            job = db.query(Job).filter(Job.id == printer.current_job_id).first()
            if job:
                job.status = JobStatus.COMPLETED
                printer.current_job_id = None
                db.commit()

    def _fail_current_job(self, db, printer):
        if printer.current_job_id:
            job = db.query(Job).filter(Job.id == printer.current_job_id).first()
            if job:
                job.status = JobStatus.FAILED
                printer.current_job_id = None
                db.commit()

    # ------------------------------------------------------------------
    # Print commands (ExecuteClient — short-lived connection)
    # ------------------------------------------------------------------

    def _get_exec_client(self) -> ExecuteClient:
        """Create a fresh ExecuteClient (it connects synchronously)."""
        return ExecuteClient(
            hostname=self.printer.ip,
            access_code=self.printer.access_code,
            serial=self.printer.serial,
        )

    def upload_file_ftp(self, filepath: str, remote_filename: str | None = None) -> str:
        """
        Upload a file to the printer via implicit FTPS (port 990).
        Bambu Lab printers use:
          - Port: 990 (implicit TLS)
          - Username: 'bblp'
          - Password: printer access_code
          - Upload dir: / (root)
        Returns the remote filename on success.
        """
        import os

        if remote_filename is None:
            remote_filename = os.path.basename(filepath)

        logger.info(
            f"Uploading '{remote_filename}' to printer {self.printer.serial} "
            f"({self.printer.ip}) via FTPS …"
        )

        # Build an SSL context that skips certificate verification
        # (Bambu printers use self-signed certs)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        local_size = os.path.getsize(filepath)

        ftp = ImplicitFTPS(context=ctx)
        try:
            # Implicit FTPS — connect directly on port 990
            ftp.connect(host=self.printer.ip, port=990, timeout=60)
            ftp.login(user="bblp", passwd=self.printer.access_code)
            # Protect the data channel as well
            ftp.prot_p()

            with open(filepath, "rb") as f:
                ftp.storbinary(f"STOR {remote_filename}", f)

            # Verify the uploaded file size matches the local file
            try:
                remote_size = ftp.size(remote_filename)
                if remote_size is not None and remote_size != local_size:
                    raise IOError(
                        f"Size mismatch after upload: "
                        f"local={local_size}, remote={remote_size}"
                    )
                logger.info(
                    f"Upload verified: {remote_filename} "
                    f"({local_size} bytes) -> {self.printer.serial}"
                )
            except IOError:
                raise
            except Exception as e:
                # SIZE command not supported — log warning but proceed
                logger.warning(f"Could not verify upload size: {e}")

            return remote_filename
        except Exception as e:
            logger.error(
                f"FTP upload failed for {self.printer.serial}: {e}"
            )
            raise
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def start_print(self, filepath: str, filename: str):
        """
        Upload the file to the printer via FTPS, then send
        the start_print MQTT command referencing the uploaded file.

        Validates that the .3mf contains sliced gcode before uploading,
        and auto-detects the correct gcode path inside the archive.
        """
        import json
        import zipfile

        logger.info(
            f"Sending start_print command to {self.printer.serial} for {filename}"
        )

        # ── Validate & extract gcode path from .3mf ──────────────
        gcode_param = "Metadata/plate_1.gcode"  # default

        if filepath.lower().endswith(".3mf"):
            try:
                with zipfile.ZipFile(filepath, "r") as zf:
                    names = zf.namelist()
                    # Find gcode files inside the archive
                    gcode_files = [
                        n for n in names
                        if n.startswith("Metadata/") and n.endswith(".gcode")
                    ]
                    if not gcode_files:
                        raise ValueError(
                            f"The .3mf file '{filename}' does not contain "
                            f"sliced gcode (no Metadata/*.gcode found). "
                            f"Please export it as a 'plate sliced file' "
                            f"from Bambu Studio, not as a project."
                        )
                    gcode_param = gcode_files[0]
                    logger.info(f"Found gcode inside .3mf: {gcode_param}")
            except zipfile.BadZipFile:
                raise ValueError(f"'{filename}' is not a valid .3mf (ZIP) file")

        try:
            # 1. Upload file to printer SD / internal storage
            self.upload_file_ftp(filepath, filename)

            # 2. Send the print command via MQTT directly
            #    (bypasses bambu-connect's hardcoded param)
            exec_client = self._get_exec_client()
            payload = json.dumps({
                "print": {
                    "sequence_id": "0",
                    "command": "project_file",
                    "param": gcode_param,
                    "subtask_name": filename,
                    "url": f"ftp://{filename}",
                    "bed_type": "auto",
                    "timelapse": False,
                    "bed_leveling": True,
                    "flow_cali": False,
                    "vibration_cali": True,
                    "layer_inspect": False,
                    "use_ams": False,
                    "profile_id": "0",
                    "project_id": "0",
                    "subtask_id": "0",
                    "task_id": "0",
                }
            })
            exec_client.send_command(payload)
            exec_client.disconnect()
            logger.info(
                f"Print started on {self.printer.serial}: "
                f"{filename} (param={gcode_param})"
            )
        except Exception as e:
            logger.error(
                f"Failed to start_print on {self.printer.serial}: {e}"
            )
            raise

    def send_gcode(self, gcode: str):
        """Send a single G-code line to the printer."""
        logger.info(f"Sending gcode to {self.printer.serial}: {gcode}")
        try:
            exec_client = self._get_exec_client()
            exec_client.send_gcode(gcode)
            exec_client.disconnect()
        except Exception as e:
            logger.error(
                f"Failed to send_gcode on {self.printer.serial}: {e}"
            )

