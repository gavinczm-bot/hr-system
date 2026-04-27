from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
from functools import wraps
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "change_this_secret_key"

DB = "hr.db"


# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT,
        department TEXT,
        supervisor_id INTEGER,
        FOREIGN KEY (supervisor_id) REFERENCES employees(id)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'employee')),
        employee_id INTEGER,
        FOREIGN KEY (employee_id) REFERENCES employees(id)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        leave_type TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'Pending',
        submitted_at TEXT NOT NULL,
        reviewed_by INTEGER,
        reviewed_at TEXT,
        review_comment TEXT,
        FOREIGN KEY (employee_id) REFERENCES employees(id),
        FOREIGN KEY (reviewed_by) REFERENCES users(id)
    )
    """)

    # Create default admin
    conn.execute("""
    INSERT OR IGNORE INTO users (username, password_hash, role, employee_id)
    VALUES (?, ?, ?, ?)
    """, ("admin", generate_password_hash("admin1234"), "admin", None))

    # Demo supervisor employee
    conn.execute("""
    INSERT OR IGNORE INTO employees (id, name, email, department, supervisor_id)
    VALUES (1, 'John Supervisor', 'john@example.com', 'Operations', NULL)
    """)

    # Demo employee reporting to John
    conn.execute("""
    INSERT OR IGNORE INTO employees (id, name, email, department, supervisor_id)
    VALUES (2, 'Mary Employee', 'mary@example.com', 'Operations', 1)
    """)

    conn.execute("""
    INSERT OR IGNORE INTO employees (id, name, email, department, supervisor_id)
    VALUES (3, 'Tom Employee', 'tom@example.com', 'Warehouse', 1)
    """)

    # Demo users
    conn.execute("""
    INSERT OR IGNORE INTO users (username, password_hash, role, employee_id)
    VALUES (?, ?, ?, ?)
    """, ("john", generate_password_hash("john1234"), "employee", 1))

    conn.execute("""
    INSERT OR IGNORE INTO users (username, password_hash, role, employee_id)
    VALUES (?, ?, ?, ?)
    """, ("mary", generate_password_hash("mary1234"), "employee", 2))

    conn.execute("""
    INSERT OR IGNORE INTO users (username, password_hash, role, employee_id)
    VALUES (?, ?, ?, ?)
    """, ("tom", generate_password_hash("tom1234"), "employee", 3))

    conn.commit()
    conn.close()


# ---------------- AUTH ----------------
def current_user():
    if "user_id" not in session:
        return None

    conn = get_db()
    user = conn.execute("""
        SELECT u.*, e.name AS employee_name, e.supervisor_id
        FROM users u
        LEFT JOIN employees e ON e.id = u.employee_id
        WHERE u.id = ?
    """, (session["user_id"],)).fetchone()
    conn.close()
    return user


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            flash("Admin access required.")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["employee_id"] = user["employee_id"]
            return redirect(url_for("dashboard"))

        flash("Login failed. Please check username/password.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------

@login_required
@app.route("/")
@login_required
def dashboard():
    user = current_user()
    conn = get_db()

    my_requests = []
    approval_requests = []
    all_pending_requests = []
    all_requests = []

    filter_start = request.args.get("start_date", "").strip()
    filter_end = request.args.get("end_date", "").strip()

    if user["employee_id"]:
        my_requests = conn.execute("""
            SELECT lr.*, e.name AS employee_name
            FROM leave_requests lr
            JOIN employees e ON e.id = lr.employee_id
            WHERE lr.employee_id = ?
            ORDER BY lr.submitted_at DESC
        """, (user["employee_id"],)).fetchall()

        approval_requests = conn.execute("""
            SELECT lr.*, e.name AS employee_name
            FROM leave_requests lr
            JOIN employees e ON e.id = lr.employee_id
            WHERE e.supervisor_id = ?
              AND lr.status = 'Pending'
            ORDER BY lr.submitted_at DESC
        """, (user["employee_id"],)).fetchall()

    if user["role"] == "admin":

        # All pending leave requests
        all_pending_requests = conn.execute("""
            SELECT lr.*, e.name AS employee_name
            FROM leave_requests lr
            JOIN employees e ON e.id = lr.employee_id
            WHERE lr.status = 'Pending'
            ORDER BY lr.submitted_at DESC
        """).fetchall()

        # All leave requests - no date filter
        if filter_start == "" and filter_end == "":
            all_requests = conn.execute("""
                SELECT lr.*, e.name AS employee_name
                FROM leave_requests lr
                JOIN employees e ON e.id = lr.employee_id
                ORDER BY lr.start_date DESC, lr.submitted_at DESC
            """).fetchall()

        # All leave requests - with date filter
        else:
            sql = """
                SELECT lr.*, e.name AS employee_name
                FROM leave_requests lr
                JOIN employees e ON e.id = lr.employee_id
                WHERE 1 = 1
            """

            params = []

            # Show leave where end date is on or after selected start date
            if filter_start != "":
                sql += " AND lr.end_date >= ? "
                params.append(filter_start)

            # Show leave where start date is on or before selected end date
            if filter_end != "":
                sql += " AND lr.start_date <= ? "
                params.append(filter_end)

            sql += """
                ORDER BY lr.start_date DESC, lr.submitted_at DESC
            """

            all_requests = conn.execute(sql, params).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        my_requests=my_requests,
        approval_requests=approval_requests,
        all_pending_requests=all_pending_requests,
        all_requests=all_requests,
        filter_start=filter_start,
        filter_end=filter_end
    )

# ---------------- LEAVE REQUEST ----------------
@app.route("/leave/new", methods=["GET", "POST"])
@login_required
def new_leave():
    user = current_user()

    if not user["employee_id"]:
        flash("Admin account is not linked to an employee. Please use an employee account to submit leave.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        leave_type = request.form["leave_type"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        reason = request.form["reason"]

        if end_date < start_date:
            flash("End date cannot be before start date.")
            return redirect(url_for("new_leave"))

        conn = get_db()
        conn.execute("""
            INSERT INTO leave_requests
            (employee_id, leave_type, start_date, end_date, reason, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, 'Pending', ?)
        """, (
            user["employee_id"],
            leave_type,
            start_date,
            end_date,
            reason,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
        conn.close()

        flash("Leave request submitted.")
        return redirect(url_for("dashboard"))

    return render_template("leave_form.html")


@app.route("/leave/<int:request_id>/review", methods=["GET", "POST"])
@login_required
def review_leave(request_id):
    user = current_user()
    conn = get_db()

    leave = conn.execute("""
        SELECT lr.*, e.name AS employee_name, e.supervisor_id
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        WHERE lr.id = ?
    """, (request_id,)).fetchone()

    if not leave:
        conn.close()
        flash("Leave request not found.")
        return redirect(url_for("dashboard"))

    is_admin = user["role"] == "admin"
    is_supervisor = user["employee_id"] and leave["supervisor_id"] == user["employee_id"]

    if not is_admin and not is_supervisor:
        conn.close()
        flash("You are not allowed to review this leave request.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        action = request.form["action"]
        comment = request.form["review_comment"]

        if action not in ["Approved", "Denied"]:
            conn.close()
            flash("Invalid action.")
            return redirect(url_for("dashboard"))

        conn.execute("""
            UPDATE leave_requests
            SET status = ?,
                reviewed_by = ?,
                reviewed_at = ?,
                review_comment = ?
            WHERE id = ?
        """, (
            action,
            user["id"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            comment,
            request_id
        ))

        conn.commit()
        conn.close()

        flash(f"Leave request {action.lower()}.")
        return redirect(url_for("dashboard"))

    conn.close()
    return render_template("review_leave.html", leave=leave)


# ---------------- ADMIN EMPLOYEE MANAGEMENT ----------------
@app.route("/admin/employees")
@login_required
@admin_required
def admin_employees():
    conn = get_db()

    employees = conn.execute("""
        SELECT e.*, s.name AS supervisor_name
        FROM employees e
        LEFT JOIN employees s ON s.id = e.supervisor_id
        ORDER BY e.name
    """).fetchall()

    users = conn.execute("""
        SELECT u.*, e.name AS employee_name
        FROM users u
        LEFT JOIN employees e ON e.id = u.employee_id
        ORDER BY u.username
    """).fetchall()

    conn.close()
    return render_template("admin_employees.html", employees=employees, users=users)


@app.route("/admin/employees/add", methods=["GET", "POST"])
@login_required
@admin_required
def add_employee():
    conn = get_db()

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        department = request.form["department"]
        supervisor_id = request.form["supervisor_id"] or None
        username = request.form["username"]
        password = request.form["password"]
        role = request.form["role"]

        cursor = conn.execute("""
            INSERT INTO employees (name, email, department, supervisor_id)
            VALUES (?, ?, ?, ?)
        """, (name, email, department, supervisor_id))

        employee_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO users (username, password_hash, role, employee_id)
            VALUES (?, ?, ?, ?)
        """, (
            username,
            generate_password_hash(password),
            role,
            employee_id
        ))

        conn.commit()
        conn.close()

        flash("Employee and user account created.")
        return redirect(url_for("admin_employees"))

    supervisors = conn.execute("""
        SELECT id, name
        FROM employees
        ORDER BY name
    """).fetchall()

    conn.close()
    return render_template("employee_form.html", supervisors=supervisors)


# ---------------- RUN ----------------
if __name__ == "__main__":
    init_db()
    app.run()