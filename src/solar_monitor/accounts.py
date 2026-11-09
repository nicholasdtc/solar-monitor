"""The routes for accounts: who you are and your own password, and for admins, everyone's accounts.

auth.LoginRequired lets only admins reach /api/users. Each route checks again, so a mistake in its
table can't open them up. Without accounts these refuse, pointing to the command line, which is
where the first account, the owner, is made.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from solar_monitor import auth, db

NO_ACCOUNTS = (
    "There are no accounts yet. To make the first, run this on the machine that runs the app: "
    "solar-monitor reset-password admin"
)
# Left out of what a new account starts with, so they open on the Dashboard.
NOT_COPIED = ("section",)

Flag = Annotated[bool, Field(strict=True)]
Password = Annotated[str, StringConstraints(max_length=auth.MAX_PASSWORD * 4)]  # Checked properly by password_problem.


class NewUserIn(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=64)]
    password: Password
    admin: Flag = False
    permissions: list[str] = []


class UserIn(BaseModel):
    admin: Flag
    permissions: list[str]


class PasswordIn(BaseModel):
    password: Password


class OwnPasswordIn(BaseModel):
    current: Password
    new: Password


def _labels(permissions: set[str] | list[str]) -> str:
    return ", ".join(label for permission, label in auth.PERMISSIONS.items() if permission in permissions)


def _permissions(asked: list[str]) -> set[str]:
    unknown = [permission for permission in asked if permission not in auth.PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"No such permission: {', '.join(unknown)}")
    return set(asked)


def _check_password(password: str) -> None:
    problem = auth.password_problem(password)
    if problem:
        raise HTTPException(status_code=400, detail=f"{problem}.")


def _is_admin(row: dict[str, object]) -> bool:
    return bool(row["admin"]) or row["username"] == auth.OWNER


def add_routes(app: FastAPI, conn: sqlite3.Connection) -> None:
    # Wrong current passwords, by account, so a session left open can't be used to guess it.
    wrong_current = auth.Throttle()

    def admin(request: Request) -> auth.User:
        user = auth.current_user(request)
        if user.local:
            raise HTTPException(status_code=409, detail=NO_ACCOUNTS)
        if not user.admin:
            raise HTTPException(status_code=403, detail="Only admins can manage accounts.")
        return user

    def target(user_id: int, action: str) -> dict[str, object]:
        """The account to change, unless it is the owner's, which no one can change from the page."""
        row = db.get_user(conn, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such account. Someone may have removed it.")
        if row["username"] == auth.OWNER:
            raise HTTPException(status_code=403, detail=f"The owner's account can't {action} here.")
        return row

    def others_are_admins(user_id: int) -> bool:
        """The backstop: someone else must still be able to manage accounts."""
        return any(_is_admin(row) for row in db.list_users(conn, auth.OWNER) if row["id"] != user_id)

    def listing(by: auth.User) -> dict[str, object]:
        sessions = db.count_sessions(conn)
        return {
            "you": by.id,
            "permissions": [{"id": permission, "label": label} for permission, label in auth.PERMISSIONS.items()],
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "owner": row["username"] == auth.OWNER,
                    "admin": _is_admin(row),
                    "permissions": row["permissions"],
                    "created": row["created"],
                    "last_login": row["last_login"],
                    "sessions": sessions.get(row["id"], 0),
                }
                for row in db.list_users(conn, auth.OWNER)
            ],
        }

    @app.get("/api/session")
    async def api_session(request: Request) -> dict[str, object]:
        return {"user": auth.current_user(request).public()}

    @app.get("/api/users")
    async def api_users(request: Request) -> dict[str, object]:
        return listing(admin(request))

    @app.post("/api/users", status_code=201)
    async def api_add_user(new: NewUserIn, request: Request) -> dict[str, object]:
        by = admin(request)
        problem = auth.username_problem(new.username)
        if problem:
            raise HTTPException(status_code=400, detail=f"{problem}.")
        _check_password(new.password)
        permissions = _permissions(new.permissions)
        if db.find_user(conn, new.username):
            raise HTTPException(status_code=409, detail="That username is taken.")
        password_hash = await asyncio.to_thread(auth.hash_password, new.password)
        try:
            user_id = db.add_user(conn, new.username, password_hash, new.admin, permissions)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="That username is taken.") from None
        owner = db.find_user(conn, auth.OWNER)
        if owner:
            db.copy_display_prefs(conn, owner["id"], user_id, leave_out=NOT_COPIED)
        given = "as an admin" if new.admin else f"with {_labels(permissions) or 'the Dashboard only'}"
        db.add_action(conn, by.who, "users", f"Added {new.username}, {given}")
        return listing(by)

    @app.put("/api/users/{user_id}")
    async def api_update_user(user_id: int, change: UserIn, request: Request) -> dict[str, object]:
        by = admin(request)
        row = target(user_id, "be changed")
        permissions = _permissions(change.permissions)
        if row["id"] == by.id and not change.admin:
            raise HTTPException(status_code=400, detail="You can't remove your own admin. Another admin can.")
        if row["admin"] and not change.admin and not others_are_admins(row["id"]):
            raise HTTPException(status_code=409, detail="There has to be at least one admin.")
        db.update_user(conn, row["id"], change.admin, permissions)
        changes = []
        if change.admin != row["admin"]:
            changes.append("made an admin" if change.admin else "no longer an admin")
        held = set(row["permissions"])
        if permissions - held:
            changes.append(f"given {_labels(permissions - held)}")
        if held - permissions:
            changes.append(f"lost {_labels(held - permissions)}")
        if changes:
            db.add_action(conn, by.who, "users", f"Changed {row['username']}: {'; '.join(changes)}")
        return listing(by)

    @app.put("/api/users/{user_id}/password")
    async def api_reset_password(user_id: int, change: PasswordIn, request: Request) -> dict[str, object]:
        by = admin(request)
        row = target(user_id, "have its password set")
        if row["id"] == by.id:
            raise HTTPException(status_code=400, detail="Change your own with Change password, in your menu.")
        _check_password(change.password)
        password_hash = await asyncio.to_thread(auth.hash_password, change.password)
        if db.get_user(conn, row["id"]) is None:
            raise HTTPException(status_code=404, detail="No such account. Someone may have removed it.")
        db.set_password_hash(conn, row["id"], password_hash)
        ended = db.delete_sessions(conn, row["id"])
        db.add_action(conn, by.who, "users", f"Set a new password for {row['username']}, logging them out ({ended})")
        return listing(by)

    @app.delete("/api/users/{user_id}")
    async def api_delete_user(user_id: int, request: Request) -> dict[str, object]:
        by = admin(request)
        row = target(user_id, "be removed")
        if row["id"] == by.id:
            raise HTTPException(status_code=400, detail="You can't remove your own account. Another admin can.")
        if row["admin"] and not others_are_admins(row["id"]):
            raise HTTPException(status_code=409, detail="There has to be at least one admin.")
        db.delete_user(conn, row["id"])
        db.add_action(conn, by.who, "users", f"Removed {row['username']}")
        return listing(by)

    @app.delete("/api/users/{user_id}/sessions")
    async def api_end_sessions(user_id: int, request: Request) -> dict[str, object]:
        by = admin(request)
        row = target(user_id, "be logged out")
        if row["id"] == by.id:
            raise HTTPException(status_code=400, detail="Use Log out everywhere, in your menu.")
        ended = db.delete_sessions(conn, row["id"])
        db.add_action(conn, by.who, "users", f"Logged {row['username']} out everywhere ({ended})")
        return listing(by)

    @app.put("/api/password")
    async def api_own_password(change: OwnPasswordIn, request: Request) -> dict[str, object]:
        """Your own password, which needs the current one. Logs you out everywhere else."""
        user = auth.current_user(request)
        if user.local:
            raise HTTPException(status_code=409, detail=NO_ACCOUNTS)
        key = str(user.id)
        wait = wrong_current.wait(key)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"Too many wrong tries. Wait {int(wait) + 1} seconds.")
        row = db.get_user(conn, user.id)
        right = await asyncio.to_thread(auth.check_password, change.current, row["password_hash"] if row else None)
        if not right:
            wrong_current.failed(key)
            raise HTTPException(status_code=403, detail="Your current password isn't right, so nothing changed.")
        wrong_current.succeeded(key)
        _check_password(change.new)
        password_hash = await asyncio.to_thread(auth.hash_password, change.new)
        db.set_password_hash(conn, user.id, password_hash)
        token = request.cookies.get(auth.COOKIE)
        ended = db.delete_sessions(conn, user.id, keep=auth.token_hash(token) if token else None)
        db.add_action(conn, user.who, "users", f"Changed their password, logging out elsewhere ({ended})")
        return {"ended": ended}
