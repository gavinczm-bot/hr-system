from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")


# ---------------- DB ----------------
def get_db():
    DATABASE_URL = os.environ.get("DATABASE_URL")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Users table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT DEFAULT 'employee',
        employee_id INTEGER
    )
    """)

    # Employees table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        id SERIAL PRIMARY KEY,
        name TEXT,
        email TEXT,
        department TEXT,
        supervisor_id INTEGER
    )
    """)

    # Leave requests table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS leave_requests (
        id SERIAL PRIMARY KEY,
        employee_id INTEGER,
        leave_type TEXT,
        start_date DATE,
        end_date DATE,
        reason TEXT,
        status TEXT DEFAULT 'Pending',
        submitted_at TIMESTAMP,
        reviewed_by INTEGER,
        reviewed_at TIMESTAMP,
        review_comment TEXT
    )
    """)

    # Default admin user
    cur.execute("""
    INSERT INTO users (username, password_hash, role)
    VALUES (%s, %s, 'admin')
    ON CONFLICT (username) DO NOTHING
    """, ("admin", generate_password_hash("admin1234")))

    conn.commit()
    cur.close()
    conn.close()


# ---------------- AUTH ----------------
def current_user():
    if "user_id" not in session:
        return None

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT u.*, e.name AS employee_name, e.supervisor_id
        FROM users u
        LEFT JOIN employees e ON e.id = u.employee_id
        WHERE u.id = %s
    """, (session["user_id"],))

    user = cur.fetchone()

    cur.close()
    conn.close()
    return user


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and user["password_hash"] and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))

        flash("Login failed")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------
@app.route("/")
@login_required
def dashboard():
    user = current_user()
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT lr.*, e.name AS employee_name
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        ORDER BY lr.submitted_at DESC
    """)
    all_requests = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("dashboard.html", all_requests=all_requests)


# ---------------- NEW LEAVE ----------------
@app.route("/leave/new", methods=["GET", "POST"])
@login_required
def new_leave():
    user = current_user()

    if not user["employee_id"]:
        flash("No employee linked")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        leave_type = request.form["leave_type"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        reason = request.form["reason"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO leave_requests
            (employee_id, leave_type, start_date, end_date, reason, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            user["employee_id"],
            leave_type,
            start_date,
            end_date,
            reason,
            datetime.now()
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("Leave submitted")
        return redirect(url_for("dashboard"))

    return render_template("leave_form.html")


# ---------------- ADMIN ADD EMPLOYEE ----------------
@app.route("/admin/add", methods=["GET", "POST"])
@login_required
def add_employee():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        department = request.form["department"]
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cur = conn.cursor()

        # create employee
        cur.execute("""
            INSERT INTO employees (name, email, department)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (name, email, department))

        employee_id = cur.fetchone()["id"]

        # create user
        cur.execute("""
            INSERT INTO users (username, password_hash, employee_id)
            VALUES (%s, %s, %s)
        """, (
            username,
            generate_password_hash(password),
            employee_id
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("Employee created")
        return redirect(url_for("dashboard"))

    return render_template("employee_form.html")


# ---------------- RUN ----------------
if __name__ == "__main__":
    init_db()
    app.run()