# main.py

from fastapi import FastAPI
import src.main

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello World"}