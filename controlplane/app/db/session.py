"""DB会话工厂。DATABASE_URL决定方言：
开发默认 sqlite:///./dev.db ｜ 生产 postgresql://... (compose注入)"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

url = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
connect_args = ({"check_same_thread": False, "timeout": 30}
                if url.startswith("sqlite") else {})
engine = create_engine(url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def init_db():
    Base.metadata.create_all(engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
