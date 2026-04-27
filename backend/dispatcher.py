import asyncio
import logging
from database import SessionLocal, Printer, PrinterStatus, Job, JobStatus
from bambu_mqtt import BambuClient

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self):
        self.running = False
        self.bambu_clients = {}  # printer_id -> BambuClient
        self._upload_tasks = {}  # job_id -> asyncio.Task (active uploads)

    def get_client(self, printer_id: int):
        if printer_id not in self.bambu_clients:
            client = BambuClient(printer_id)
            client.connect()
            self.bambu_clients[printer_id] = client
        return self.bambu_clients[printer_id]

    async def start(self):
        self.running = True
        logger.info("Dispatcher started.")
        # Ensure clients for all existing printers are created
        self.initialize_clients()

        while self.running:
            await self.poll_printer_status()
            await self.check_queue()
            await asyncio.sleep(5)  # Poll every 5 seconds

    async def poll_printer_status(self):
        """
        Request a full status dump from every connected printer.
        ExecuteClient.dump_info() triggers the printer to push a complete
        status MQTT message, which is handled by BambuClient._on_status_update.
        Runs synchronously but is fast (fire-and-forget MQTT publish).
        """
        for printer_id, client in list(self.bambu_clients.items()):
            try:
                exec_client = client._get_exec_client()
                exec_client.dump_info()
                exec_client.disconnect()
                logger.debug(f"Polled status for printer_id={printer_id}")
            except Exception as e:
                logger.warning(f"Failed to poll status for printer_id={printer_id}: {e}")
                # Mark printer offline if we can't reach it
                client.update_status(PrinterStatus.OFFLINE)

    def stop(self):
        self.running = False
        # Cancel any in-progress upload tasks
        for task in self._upload_tasks.values():
            task.cancel()
        for client in self.bambu_clients.values():
            client.disconnect()
        logger.info("Dispatcher stopped.")

    def initialize_clients(self):
        db = SessionLocal()
        printers = db.query(Printer).all()
        for printer in printers:
            self.get_client(printer.id)
        db.close()

    async def check_queue(self):
        db = SessionLocal()
        try:
            # 1. Find all QUEUED jobs
            queued_jobs = (
                db.query(Job)
                .filter(Job.status == JobStatus.QUEUED)
                .order_by(Job.created_at)
                .all()
            )

            if not queued_jobs:
                return  # Nothing to do

            # 2. Find all READY printers
            ready_printers = (
                db.query(Printer)
                .filter(Printer.status == PrinterStatus.READY)
                .all()
            )

            # 3. Match jobs to printers
            for job in queued_jobs:
                if not ready_printers:
                    break  # No more available printers

                printer = ready_printers.pop(0)  # Take the first ready printer

                # Assign job — mark as UPLOADING (not PRINTING yet)
                logger.info(
                    f"Assigning job {job.id} ({job.filename}) "
                    f"to printer {printer.name} — starting upload"
                )
                job.printer_id = printer.id
                job.status = JobStatus.UPLOADING
                printer.current_job_id = job.id
                printer.status = PrinterStatus.PRINTING
                db.commit()

                # Spawn background task for FTP upload + print command
                task = asyncio.create_task(
                    self._upload_and_print_async(
                        job.id, printer.id, job.filepath, job.filename
                    )
                )
                self._upload_tasks[job.id] = task

        except Exception as e:
            logger.error(f"Error in dispatcher loop: {e}")
        finally:
            db.close()

    async def _upload_and_print_async(
        self, job_id: int, printer_id: int, filepath: str, filename: str
    ):
        """
        Wrapper that runs the blocking FTP upload in a thread pool
        so it doesn't block the async event loop.
        """
        try:
            await asyncio.to_thread(
                self._upload_and_print_sync,
                job_id, printer_id, filepath, filename,
            )
        except asyncio.CancelledError:
            logger.warning(f"Upload task for job {job_id} was cancelled")
            self._mark_job_failed(job_id, printer_id)
        except Exception as e:
            logger.error(f"Upload task for job {job_id} failed: {e}")
            self._mark_job_failed(job_id, printer_id)
        finally:
            self._upload_tasks.pop(job_id, None)

    def _upload_and_print_sync(
        self, job_id: int, printer_id: int, filepath: str, filename: str
    ):
        """
        Blocking worker that:
        1. Uploads the file to the printer via FTP
        2. Sends the print command via MQTT
        3. Updates the job status to PRINTING on success
        Runs inside a thread pool — safe to block here.
        """
        client = self.get_client(printer_id)

        try:
            # This does FTP upload + MQTT print command
            client.start_print(filepath, filename)

            # Success — mark job as PRINTING
            db = SessionLocal()
            try:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job:
                    job.status = JobStatus.PRINTING
                    db.commit()
                    logger.info(
                        f"Job {job_id} uploaded and printing on printer {printer_id}"
                    )
            finally:
                db.close()

        except Exception as e:
            logger.error(
                f"Failed to upload/start job {job_id} "
                f"on printer {printer_id}: {e}"
            )
            self._mark_job_failed(job_id, printer_id)
            raise

    def _mark_job_failed(self, job_id: int, printer_id: int):
        """Mark a job as FAILED and free the printer."""
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = JobStatus.FAILED
            printer = (
                db.query(Printer).filter(Printer.id == printer_id).first()
            )
            if printer and printer.current_job_id == job_id:
                printer.current_job_id = None
                printer.status = PrinterStatus.READY
            db.commit()
            logger.info(f"Job {job_id} marked FAILED, printer {printer_id} freed")
        finally:
            db.close()


dispatcher = Dispatcher()
