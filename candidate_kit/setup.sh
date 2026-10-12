#!/usr/bin/env bash
set -e

echo "Creating virtual environment .venv..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

echo "Activating .venv and installing requirements..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

echo ""
echo "Setup finished successfully!"
echo "To activate: source .venv/bin/activate"
echo "To run the app: uvicorn app.main:app --port 8000 --reload"
