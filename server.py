import os
import threading
from fastapi import FastAPI
from uvicorn.main import Server, Config
import subprocess

app = FastAPI(title="GUN4FUN Keepalive")

bot_proc = None

def start_bot():
    # Lanza el bot en un proceso hijo separado
    # TOKEN y TZ vienen de variables de entorno
    subprocess.Popen(["python", "bot.py"])

@app.on_event("startup")
async def startup():
    # Arranca el bot en hilo aparte para no bloquear FastAPI
    t = threading.Thread(target=start_bot, daemon=True)
    t.start()

@app.get("/")
def root():
    return {"ok": True, "service": "gun4fun-trivia", "status": "alive"}

@app.get("/health")
def health_get():
    return {"status": "healthy"}

@app.head("/health")
def health_head():
    return {}
