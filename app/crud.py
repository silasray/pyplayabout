from sqlalchemy.orm import Session


def get_items(db: Session):
    return []


def get_item(db: Session, item_id):
    return None


def create_item(db: Session, item):
    return item


def update_item(db: Session, item_id, item):
    return item


def delete_item(db: Session, item_id):
    return False

