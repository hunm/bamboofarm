from sqlalchemy import create_engine, Column, Integer, String, DateTime, Enum, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import enum

DATABASE_URL = "sqlite:///./farm.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class PrinterStatus(str, enum.Enum):
    OFFLINE = "OFFLINE"
    READY = "READY"
    PRINTING = "PRINTING"
    FINISHED = "FINISHED" # Transitional
    WAITING_CLEAN = "WAITING_CLEAN"
    ERROR = "ERROR"

class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    UPLOADING = "UPLOADING"
    PRINTING = "PRINTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class Printer(Base):
    __tablename__ = "printers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    ip = Column(String, unique=True)
    serial = Column(String, unique=True)
    access_code = Column(String)
    status = Column(Enum(PrinterStatus), default=PrinterStatus.OFFLINE)
    current_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    # Hardware info (populated from MQTT)
    model = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    ams_installed = Column(Boolean, default=False)
    nozzle_diameter = Column(String, nullable=True)  # e.g. "0.4"

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    filepath = Column(String)
    status = Column(Enum(JobStatus), default=JobStatus.QUEUED)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    progress = Column(Integer, default=0) # 0 to 100

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
