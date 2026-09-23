from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.schemas import ItemCreate, ItemRead, ItemUpdate

router = APIRouter(prefix="/api/v1", tags=["items"])


@router.get("/health")
def health_check():
    return {"status": "ok", "service": "pyplayabout-api"}


@router.get("/items", response_model=list[ItemRead])
def read_items(db: Session = Depends(get_db)):
    return crud.get_items(db)


@router.get("/items/{item_id}", response_model=ItemRead)
def read_item(item_id: int, db: Session = Depends(get_db)):
    item = crud.get_item(db, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
def create_item_endpoint(item: ItemCreate, db: Session = Depends(get_db)):
    return crud.create_item(db, item)


@router.put("/items/{item_id}", response_model=ItemRead)
def update_item_endpoint(item_id: int, item: ItemUpdate, db: Session = Depends(get_db)):
    updated_item = crud.update_item(db, item_id, item)
    if updated_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return updated_item


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item_endpoint(item_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_item(db, item_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return None
