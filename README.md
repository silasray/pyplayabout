# PyPlayabout FastAPI app

This project is a simple two-tier FastAPI application:

- API tier: FastAPI endpoints for CRUD operations
- Data tier: SQLite database managed by SQLAlchemy

## Project structure

- `app/` - application package
- `app/main.py` - FastAPI entry point
- `app/api/routes.py` - API routes
- `app/database.py` - database connection and session setup
- `app/models.py` - SQLAlchemy models
- `app/schemas.py` - Pydantic request/response models
- `app/crud.py` - database access helpers

## Run locally

1. Create a virtual environment if needed.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Start the server:

   ```bash
   uvicorn app.main:app --reload
   ```

4. Open the API docs at:

   ```text
   http://127.0.0.1:8000/docs
   ```

## Example endpoints

- `GET /` - app info
- `GET /api/v1/health` - health check
- `GET /api/v1/items` - list items
- `POST /api/v1/items` - create an item
