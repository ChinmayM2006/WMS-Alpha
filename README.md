# WMS Alpha

Minimal Warehouse Management System (WMS) demo built with Flask and SQLite. It models inbound receiving, storage, picking, and shipping with a simple rack layout.

## Features
- Rack grid with zones and slot occupancy
- SKU catalog validation (master catalog)
- Add new SKU and receive stock for existing SKUs
- Dock staging and putaway flow
- FIFO/LIFO picking toggle
- Batch pick list with route ordering
- Recent activity log

## Run
1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install flask
   ```
3. Start the app:
   ```bash
   python app.py
   ```
4. Open in your browser:
   ```
   http://127.0.0.1:5000/
   ```

## Workflow (short)
Receive -> (Dock) -> Putaway -> Pick -> Ship

## Notes
- The app seeds sample data on first run.
- Use "Add New SKU" only for new products in the master catalog.
- Use "Receive Stock" for existing SKUs.
