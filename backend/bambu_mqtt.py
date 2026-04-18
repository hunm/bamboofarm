import json
import logging
import ssl
import paho.mqtt.client as mqtt
from database import SessionLocal, Printer, PrinterStatus, Job, JobStatus

logger = logging.getLogger(__name__)

class BambuClient:
    def __init__(self, printer_id: int):
        self.printer_id = printer_id
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        # We need to get printer details from DB
        db = SessionLocal()
        self.printer = db.query(Printer).filter(Printer.id == printer_id).first()
        db.close()
        
        if not self.printer:
            raise ValueError(f"Printer {printer_id} not found in DB")
            
        self.client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)
        self.client.tls_insecure_set(True)
        self.client.username_pw_set("bblp", self.printer.access_code)
        
    def connect(self):
        logger.info(f"Connecting to Bambu Printer {self.printer.serial} at {self.printer.ip}")
        try:
            self.client.connect(self.printer.ip, 8883, 60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"Failed to connect to printer {self.printer.serial}: {e}")
            self.update_status(PrinterStatus.OFFLINE)

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info(f"Connected to printer {self.printer.serial}")
            self.client.subscribe(f"device/{self.printer.serial}/report")
            self.update_status(PrinterStatus.READY) # Assume ready if we can connect, state machine will verify
        else:
            logger.error(f"Connection failed with code {rc}")
            self.update_status(PrinterStatus.OFFLINE)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if "print" in payload:
                print_data = payload["print"]
                if "gcode_state" in print_data:
                    state = print_data["gcode_state"]
                    self.handle_gcode_state(state)
                
                if "mc_percent" in print_data:
                    progress = print_data["mc_percent"]
                    self.update_progress(progress)
                    
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def handle_gcode_state(self, state: str):
        db = SessionLocal()
        printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
        if not printer:
            db.close()
            return

        current_status = printer.status
        new_status = current_status
        
        if state == "IDLE":
            if current_status == PrinterStatus.PRINTING:
                # Finished print, now waiting for clean
                new_status = PrinterStatus.WAITING_CLEAN
                self.complete_current_job(db, printer)
            elif current_status == PrinterStatus.FINISHED:
                 new_status = PrinterStatus.WAITING_CLEAN
        elif state == "RUNNING":
            new_status = PrinterStatus.PRINTING
        elif state == "FINISH":
            new_status = PrinterStatus.WAITING_CLEAN
            self.complete_current_job(db, printer)
        elif state == "FAILED":
            new_status = PrinterStatus.ERROR
            self.fail_current_job(db, printer)

        if new_status != current_status:
            logger.info(f"Printer {self.printer.serial} state changed: {current_status} -> {new_status}")
            printer.status = new_status
            db.commit()
            
        db.close()

    def update_status(self, status: PrinterStatus):
        db = SessionLocal()
        printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
        if printer and printer.status != status:
            # Do not overwrite WAITING_CLEAN if it's already there and we re-connect
            if not (printer.status == PrinterStatus.WAITING_CLEAN and status == PrinterStatus.READY):
                printer.status = status
                db.commit()
        db.close()

    def update_progress(self, progress: int):
        db = SessionLocal()
        printer = db.query(Printer).filter(Printer.id == self.printer_id).first()
        if printer and printer.current_job_id:
            job = db.query(Job).filter(Job.id == printer.current_job_id).first()
            if job and job.progress != progress:
                job.progress = progress
                db.commit()
        db.close()

    def complete_current_job(self, db, printer):
        if printer.current_job_id:
            job = db.query(Job).filter(Job.id == printer.current_job_id).first()
            if job:
                job.status = JobStatus.COMPLETED
                printer.current_job_id = None
                db.commit()

    def fail_current_job(self, db, printer):
        if printer.current_job_id:
            job = db.query(Job).filter(Job.id == printer.current_job_id).first()
            if job:
                job.status = JobStatus.FAILED
                printer.current_job_id = None
                db.commit()

    def start_print(self, filepath: str, filename: str):
        # In a real implementation:
        # 1. Connect to FTPS (port 990) with bblp / access_code
        # 2. Upload filepath to /data/upload/
        # 3. Send MQTT command to start print
        
        # Here we mock the MQTT command
        logger.info(f"Mocking FTPS upload of {filepath} to printer {self.printer.serial}")
        
        command = {
            "print": {
                "sequence_id": "1",
                "command": "project_file",
                "param": f"/data/upload/{filename}",
                "project_id": "0",
                "profile_id": "0",
                "task_id": "0",
                "subtask_id": "0",
                "subtask_name": filename,
                "file": f"/data/upload/{filename}",
                "ame": filename,
                "md5": "0"
            }
        }
        
        topic = f"device/{self.printer.serial}/request"
        self.client.publish(topic, json.dumps(command))
        logger.info(f"Sent print command to {self.printer.serial} for {filename}")
        
        # We manually update status for simulation purposes if needed, 
        # but the printer should report RUNNING soon after.
