import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List

from database import init_db, get_db, Printer, Job, PrinterStatus, JobStatus
from farm_mqtt import farm_mqtt_client
from dispatcher import dispatcher

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

# API Endpoints
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
                    "status": job.status.value
                }
        result.append({
            "id": p.id,
            "name": p.name,
            "ip": p.ip,
            "serial": p.serial,
            "status": p.status.value,
            "current_job": job_data
        })
    return result

@app.post("/api/printers")
def add_printer(name: str, ip: str, serial: str, access_code: str, db: Session = Depends(get_db)):
    printer = Printer(name=name, ip=ip, serial=serial, access_code=access_code)
    db.add(printer)
    db.commit()
    db.refresh(printer)
    # Tell dispatcher to initialize this client
    dispatcher.get_client(printer.id)
    return {"status": "success", "id": printer.id}

@app.post("/api/upload")
async def upload_job(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filepath = os.path.join(UPLOAD_DIR, file.filename)
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)
        
    job = Job(filename=file.filename, filepath=filepath)
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"status": "success", "job_id": job.id}

@app.get("/api/jobs")
def get_jobs(db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).all()
    return [{
        "id": j.id,
        "filename": j.filename,
        "status": j.status.value,
        "progress": j.progress,
        "printer_id": j.printer_id
    } for j in jobs]

# Mount static files for frontend
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
os.makedirs(frontend_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/")
def read_root():
    return FileResponse(os.path.join(frontend_dir, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
