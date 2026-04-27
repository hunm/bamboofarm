import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from typing import List

from database import init_db, get_db, Printer, Job, PrinterStatus, JobStatus
from farm_mqtt import farm_mqtt_client
from dispatcher import dispatcher

logger = logging.getLogger(__name__)

# Ensure upload directory exists
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    farm_mqtt_client.start()
    dispatcher_task = asyncio.create_task(dispatcher.start())
    yield
    # Shutdown
    dispatcher.stop()
    await dispatcher_task
    farm_mqtt_client.stop()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Printers API
# ---------------------------------------------------------------------------

@app.get("/api/printers")
def get_printers(db: Session = Depends(get_db)):
    printers = db.query(Printer).all()
    result = []
    for p in printers:
        job_data = None
        if p.current_job_id:
            job = db.query(Job).filter(Job.id == p.current_job_id).first()
            if job:
                job_data = {
                    "id": job.id,
                    "filename": job.filename,
                    "progress": job.progress,
                    "status": job.status.value,
                }

        # Pull live print data from in-memory BambuClient
        print_info = None
        if p.id in dispatcher.bambu_clients:
            print_info = dict(dispatcher.bambu_clients[p.id].print_info)

        result.append(
            {
                "id": p.id,
                "name": p.name,
                "ip": p.ip,
                "serial": p.serial,
                "status": p.status.value,
                "current_job": job_data,
                "print_info": print_info,
            }
        )
    return result


@app.post("/api/printers")
def add_printer(
    name: str,
    ip: str,
    serial: str,
    access_code: str,
    db: Session = Depends(get_db),
):
    printer = Printer(name=name, ip=ip, serial=serial, access_code=access_code)
    db.add(printer)
    db.commit()
    db.refresh(printer)
    # Initialize bambu-connect client for this printer
    dispatcher.get_client(printer.id)
    return {"status": "success", "id": printer.id}


@app.post("/api/printers/{printer_id}/ready")
def set_printer_ready(printer_id: int, db: Session = Depends(get_db)):
    """Manually force printer status to READY and clear current job."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    if printer.status != PrinterStatus.WAITING_CLEAN:
        raise HTTPException(status_code=400, detail="Printer must be in WAITING_CLEAN status")
    
    printer.status = PrinterStatus.READY
    printer.current_job_id = None
    db.commit()
    
    return {"status": "success", "printer_id": printer.id}


# ---------------------------------------------------------------------------
# Printer Details / Hardware Info
# ---------------------------------------------------------------------------

@app.get("/api/printers/{printer_id}/details")
def get_printer_details(printer_id: int, db: Session = Depends(get_db)):
    """Return full printer info: DB fields + live hardware data from MQTT."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Base info from DB
    # Detect model from serial as fallback
    model = printer.model
    if not model:
        from bambu_mqtt import BambuClient
        model = BambuClient._detect_model(printer.serial)

    result = {
        "id": printer.id,
        "name": printer.name,
        "ip": printer.ip,
        "serial": printer.serial,
        "status": printer.status.value,
        "model": model,
        "firmware_version": printer.firmware_version,
        "ams_installed": printer.ams_installed,
        "nozzle_diameter": printer.nozzle_diameter,
    }

    # Merge live hardware info from BambuClient
    client = dispatcher.bambu_clients.get(printer_id)
    if client:
        hw = dict(client.hw_info)
        # Ensure model is always set
        if not hw.get("model"):
            hw["model"] = model
        result["hw_info"] = hw
        result["print_info"] = dict(client.print_info)
    else:
        result["hw_info"] = {"model": model}
        result["print_info"] = None

    return result


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------

@app.get("/api/printers/{printer_id}/print_status")
def get_print_status(printer_id: int, db: Session = Depends(get_db)):
    """Return live print telemetry for a single printer (updated every ~5 s)."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    client = dispatcher.bambu_clients.get(printer_id)
    if not client:
        raise HTTPException(status_code=503, detail="Printer client not connected")

    return dict(client.print_info)


def _mjpeg_generator(ip: str, access_code: str):
    """
    Generator that yields MJPEG boundary frames.
    Uses bambu-connect CameraClient under the hood.
    Runs in a thread (called via StreamingResponse).
    """
    from bambu_connect.CameraClient import CameraClient
    import queue
    import threading

    frame_queue: queue.Queue = queue.Queue(maxsize=2)
    streaming_flag = {"active": True}

    def _callback(jpeg_bytes: bytes):
        if not streaming_flag["active"]:
            return
        # Drop frames if consumer is slow (non-blocking put)
        try:
            frame_queue.put_nowait(jpeg_bytes)
        except queue.Full:
            pass

    camera = CameraClient(hostname=ip, access_code=access_code)

    stream_thread = threading.Thread(
        target=camera.capture_stream,
        args=(_callback,),
        daemon=True,
    )
    camera.streaming = True
    stream_thread.start()

    try:
        while True:
            try:
                frame = frame_queue.get(timeout=5.0)
            except queue.Empty:
                # Timeout — client probably disconnected
                break

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
    finally:
        # Signal CameraClient to stop
        camera.streaming = False
        streaming_flag["active"] = False
        stream_thread.join(timeout=3)


@app.get("/api/printers/{printer_id}/camera")
def camera_stream(printer_id: int, db: Session = Depends(get_db)):
    """MJPEG live stream from the printer camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    return StreamingResponse(
        _mjpeg_generator(printer.ip, printer.access_code),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/printers/{printer_id}/snapshot")
def camera_snapshot(printer_id: int, db: Session = Depends(get_db)):
    """Single JPEG snapshot from the printer camera."""
    from bambu_connect.CameraClient import CameraClient

    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    try:
        camera = CameraClient(hostname=printer.ip, access_code=printer.access_code)
        jpeg = camera.capture_frame()
        if not jpeg:
            raise HTTPException(status_code=503, detail="Could not capture frame")
        return StreamingResponse(
            iter([jpeg]),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as e:
        logger.error(f"Snapshot error for printer {printer_id}: {e}")
        raise HTTPException(status_code=503, detail=str(e))


# ---------------------------------------------------------------------------
# Jobs API
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_job(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filepath = os.path.join(UPLOAD_DIR, file.filename)
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    # Check if .3mf contains sliced gcode (warn if not, but allow upload)
    warning = None
    if file.filename.lower().endswith(".3mf"):
        import zipfile
        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                has_gcode = any(
                    n.startswith("Metadata/") and n.endswith(".gcode")
                    for n in zf.namelist()
                )
                if not has_gcode:
                    warning = (
                        "This .3mf file does not contain sliced gcode. "
                        "Printing will fail. Please export a "
                        "'plate sliced file' from Bambu Studio."
                    )
        except zipfile.BadZipFile:
            warning = "This file does not appear to be a valid .3mf archive."

    job = Job(filename=file.filename, filepath=filepath)
    db.add(job)
    db.commit()
    db.refresh(job)
    result = {"status": "success", "job_id": job.id}
    if warning:
        result["warning"] = warning
    return result


@app.get("/api/jobs")
def get_jobs(db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).all()
    result = []
    for j in jobs:
        remaining = None
        progress = j.progress
        if j.status == JobStatus.PRINTING and j.printer_id:
            client = dispatcher.bambu_clients.get(j.printer_id)
            if client:
                remaining = client.print_info.get("remaining_time_min")
                live_progress = client.print_info.get("progress")
                if live_progress is not None:
                    progress = live_progress
        result.append({
            "id": j.id,
            "filename": j.filename,
            "status": j.status.value,
            "progress": progress,
            "printer_id": j.printer_id,
            "remaining_time_min": remaining,
        })
    return result


@app.post("/api/printers/{printer_id}/upload")
async def upload_to_printer(
    printer_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a 3MF/G-code file directly to a printer via FTPS."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Save file locally first
    filepath = os.path.join(UPLOAD_DIR, file.filename)
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    # Upload to printer via FTP
    try:
        client = dispatcher.get_client(printer_id)
        client.upload_file_ftp(filepath, file.filename)
    except Exception as e:
        logger.error(f"FTP upload to printer {printer_id} failed: {e}")
        raise HTTPException(status_code=500, detail=f"FTP upload failed: {e}")

    return {"status": "success", "filename": file.filename, "printer_id": printer_id}


# ---------------------------------------------------------------------------
# Static / Frontend
# ---------------------------------------------------------------------------

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
os.makedirs(frontend_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def read_root():
    return FileResponse(os.path.join(frontend_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
