
import os
import subprocess
from fastapi import FastAPI

app = FastAPI(title="GUN4FUN Keepalive")

bot_proc = None

@app.on_event("startup")
async def startup():
    global bot_proc
    bot_proc = subprocess.Popen(["python", "bot.py"])

@app.on_event("shutdown")
def shutdown():
    global bot_proc
    if bot_proc and bot_proc.poll() is None:
        try:
            bot_proc.terminate()
        except Exception:
            pass

@app.get("/")
def root():
    return {"ok": True, "service": "gun4fun-trivia", "status": "alive"}

@app.get("/health")
def health():
    return {"status": "healthy"}
