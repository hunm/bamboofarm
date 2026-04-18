import asyncio
import logging
from database import SessionLocal, Printer, PrinterStatus, Job, JobStatus
from bambu_mqtt import BambuClient

logger = logging.getLogger(__name__)

class Dispatcher:
    def __init__(self):
        self.running = False
        self.bambu_clients = {} # printer_id -> BambuClient

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
            await self.check_queue()
            await asyncio.sleep(5) # Poll every 5 seconds

    def stop(self):
        self.running = False
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
            queued_jobs = db.query(Job).filter(Job.status == JobStatus.QUEUED).order_by(Job.created_at).all()
            
            if not queued_jobs:
                return # Nothing to do
                
            # 2. Find all READY printers
            ready_printers = db.query(Printer).filter(Printer.status == PrinterStatus.READY).all()
            
            # 3. Match jobs to printers
            for job in queued_jobs:
                if not ready_printers:
                    break # No more available printers
                    
                printer = ready_printers.pop(0) # Take the first ready printer
                
                # Assign job
                logger.info(f"Assigning job {job.id} ({job.filename}) to printer {printer.name}")
                job.printer_id = printer.id
                job.status = JobStatus.PRINTING
                printer.current_job_id = job.id
                printer.status = PrinterStatus.PRINTING
                db.commit()
                
                # Trigger print via Bambu API
                client = self.get_client(printer.id)
                client.start_print(job.filepath, job.filename)
                
        except Exception as e:
            logger.error(f"Error in dispatcher loop: {e}")
        finally:
            db.close()

dispatcher = Dispatcher()
