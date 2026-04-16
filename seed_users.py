"""Seed user accounts for the certificate web application.

Usage:
    python seed_users.py add <username> <password>
    python seed_users.py list
    python seed_users.py remove <username>
"""
import sys
from pathlib import Path

import bcrypt

# Set up Flask app context to access the database
from app import create_app
from models import db, User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def add_user(username: str, password: str) -> None:
    existing = User.query.filter_by(username=username).first()
    if existing:
        print(f"User '{username}' already exists.")
        return
    user = User(username=username, password_hash=hash_password(password))
    db.session.add(user)
    db.session.commit()
    print(f"Created user '{username}'.")


def list_users() -> None:
    users = User.query.all()
    if not users:
        print("No users found.")
        return
    for u in users:
        print(f"  {u.id}. {u.username} (created {u.created_at})")


def remove_user(username: str) -> None:
    user = User.query.filter_by(username=username).first()
    if not user:
        print(f"User '{username}' not found.")
        return
    db.session.delete(user)
    db.session.commit()
    print(f"Removed user '{username}'.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    app = create_app()
    with app.app_context():
        db.create_all()

        cmd = sys.argv[1]
        if cmd == "add" and len(sys.argv) == 4:
            add_user(sys.argv[2], sys.argv[3])
        elif cmd == "list":
            list_users()
        elif cmd == "remove" and len(sys.argv) == 3:
            remove_user(sys.argv[2])
        else:
            print(__doc__)
            sys.exit(1)


if __name__ == "__main__":
    main()
