from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import os
import io
import psycopg2
from psycopg2.extras import RealDictCursor
from functools import wraps
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key")


# ---------------- DB ----------------
def get_db():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is missing.")

    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee (
            id SERIAL PRIMARY KEY,
            name TEXT,
            department TEXT,
            salary NUMERIC
        )
    """)

    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'employee'")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_id INTEGER")

    cur.execute("ALTER TABLE employee ADD COLUMN IF NOT EXISTS email TEXT")
    cur.execute("ALTER TABLE employee ADD COLUMN IF NOT EXISTS supervisor_id INTEGER")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id SERIAL PRIMARY KEY,
            employee_id INTEGER NOT NULL,
            leave_type TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',
            submitted_at TIMESTAMP NOT NULL,
            reviewed_by INTEGER,
            reviewed_at TIMESTAMP,
            review_comment TEXT
        )
    """)

    cur.execute("ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS start_time TIME")
    cur.execute("ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS end_time TIME")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS leave_attachments (
            id SERIAL PRIMARY KEY,
            leave_request_id INTEGER NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
            file_name TEXT NOT NULL,
            content_type TEXT,
            file_data BYTEA NOT NULL,
            uploaded_at TIMESTAMP NOT NULL
        )
    """)

    cur.execute("""
        INSERT INTO users (username, password, role, employee_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username)
        DO UPDATE SET
            password = EXCLUDED.password,
            role = EXCLUDED.role
    """, ("admin", "paper1234", "admin", None))

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
        SELECT
            u.id,
            u.username,
            u.role,
            u.employee_id,
            e.name AS employee_name,
            e.email AS employee_email,
            e.department,
            e.supervisor_id
        FROM users u
        LEFT JOIN employee e ON e.id = u.employee_id
        WHERE u.id = %s
    """, (session["user_id"],))

    user = cur.fetchone()

    cur.close()
    conn.close()

    return user


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


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


# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM users
            WHERE username = %s
              AND password = %s
        """, (username, password))

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["employee_id"] = user["employee_id"]
            return redirect(url_for("dashboard"))

        flash("Login failed.")

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

    my_requests = []
    approval_requests = []

    if user["employee_id"]:
        cur.execute("""
            SELECT
                lr.*,
                e.name AS employee_name,
                reviewer.username AS reviewer_name,
                (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count
            FROM leave_requests lr
            JOIN employee e ON e.id = lr.employee_id
            LEFT JOIN users reviewer ON reviewer.id = lr.reviewed_by
            WHERE lr.employee_id = %s
            ORDER BY lr.submitted_at DESC
        """, (user["employee_id"],))

        my_requests = cur.fetchall()

        cur.execute("""
            SELECT
                lr.*,
                e.name AS employee_name,
                (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count
            FROM leave_requests lr
            JOIN employee e ON e.id = lr.employee_id
            WHERE e.supervisor_id = %s
              AND lr.status = 'Pending'
            ORDER BY lr.submitted_at DESC
        """, (user["employee_id"],))

        approval_requests = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "dashboard.html",
        my_requests=my_requests,
        approval_requests=approval_requests
    )


# ---------------- ADMIN LEAVE REQUESTS ----------------
@app.route("/admin/leave-requests")
@login_required
@admin_required
def admin_leave_requests():
    filter_start = request.args.get("start_date", "").strip()
    filter_end = request.args.get("end_date", "").strip()
    filter_status = request.args.get("status", "").strip()

    conn = get_db()
    cur = conn.cursor()

    sql = """
        SELECT
            lr.*,
            e.name AS employee_name,
            e.department,
            reviewer.username AS reviewer_name,
            (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        LEFT JOIN users reviewer ON reviewer.id = lr.reviewed_by
        WHERE 1 = 1
    """

    params = []

    if filter_start:
        sql += " AND lr.end_date >= %s "
        params.append(filter_start)

    if filter_end:
        sql += " AND lr.start_date <= %s "
        params.append(filter_end)

    if filter_status:
        sql += " AND lr.status = %s "
        params.append(filter_status)

    sql += """
        ORDER BY lr.start_date DESC, lr.submitted_at DESC
    """

    cur.execute(sql, params)
    all_requests = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "admin_leave_requests.html",
        all_requests=all_requests,
        filter_start=filter_start,
        filter_end=filter_end,
        filter_status=filter_status
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
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        reason = request.form["reason"]
        attachment = request.files.get("attachment")

        if end_date < start_date:
            flash("End date cannot be before start date.")
            return redirect(url_for("new_leave"))

        if not start_time or not end_time:
            flash("Start time and finish time are required.")
            return redirect(url_for("new_leave"))

        if start_date == end_date and end_time <= start_time:
            flash("Finish time must be after start time when leave is on the same day.")
            return redirect(url_for("new_leave"))

        conn = get_db()
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO leave_requests
                    (employee_id, leave_type, start_date, end_date, start_time, end_time, reason, status, submitted_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'Pending', %s)
                RETURNING id
            """, (
                user["employee_id"],
                leave_type,
                start_date,
                end_date,
                start_time,
                end_time,
                reason,
                datetime.now()
            ))

            leave_request_id = cur.fetchone()["id"]

            if attachment and attachment.filename:
                file_name = os.path.basename(attachment.filename)
                file_data = attachment.read()

                if file_data:
                    cur.execute("""
                        INSERT INTO leave_attachments
                            (leave_request_id, file_name, content_type, file_data, uploaded_at)
                        VALUES
                            (%s, %s, %s, %s, %s)
                    """, (
                        leave_request_id,
                        file_name,
                        attachment.content_type,
                        psycopg2.Binary(file_data),
                        datetime.now()
                    ))

            conn.commit()
            flash("Leave request submitted.")

        except Exception as e:
            conn.rollback()
            flash("Error submitting leave request: " + str(e))

        cur.close()
        conn.close()

        return redirect(url_for("dashboard"))

    return render_template("leave_form.html")


@app.route("/leave/<int:request_id>/view")
@login_required
def view_leave(request_id):
    user = current_user()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            lr.*,
            e.name AS employee_name,
            e.email,
            e.department,
            e.supervisor_id,
            reviewer.username AS reviewer_name
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        LEFT JOIN users reviewer ON reviewer.id = lr.reviewed_by
        WHERE lr.id = %s
    """, (request_id,))

    leave = cur.fetchone()

    attachments = []
    if leave:
        cur.execute("""
            SELECT id, file_name, content_type, uploaded_at
            FROM leave_attachments
            WHERE leave_request_id = %s
            ORDER BY uploaded_at DESC, id DESC
        """, (request_id,))
        attachments = cur.fetchall()

    cur.close()
    conn.close()

    if not leave:
        flash("Leave request not found.")
        return redirect(url_for("dashboard"))

    is_admin = user["role"] == "admin"
    is_owner = user["employee_id"] and leave["employee_id"] == user["employee_id"]
    is_supervisor = user["employee_id"] and leave["supervisor_id"] == user["employee_id"]

    if not is_admin and not is_owner and not is_supervisor:
        flash("You are not allowed to view this request.")
        return redirect(url_for("dashboard"))

    return render_template(
        "view_leave.html",
        leave=leave,
        attachments=attachments,
        can_review=(is_admin or is_supervisor)
    )


@app.route("/leave/<int:request_id>/attachment/<int:attachment_id>")
@login_required
def download_leave_attachment(request_id, attachment_id):
    user = current_user()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            lr.employee_id,
            e.supervisor_id,
            la.file_name,
            la.content_type,
            la.file_data
        FROM leave_attachments la
        JOIN leave_requests lr ON lr.id = la.leave_request_id
        JOIN employee e ON e.id = lr.employee_id
        WHERE la.id = %s
          AND lr.id = %s
    """, (attachment_id, request_id))

    attachment = cur.fetchone()

    cur.close()
    conn.close()

    if not attachment:
        flash("Attachment not found.")
        return redirect(url_for("view_leave", request_id=request_id))

    is_admin = user["role"] == "admin"
    is_owner = user["employee_id"] and attachment["employee_id"] == user["employee_id"]
    is_supervisor = user["employee_id"] and attachment["supervisor_id"] == user["employee_id"]

    if not is_admin and not is_owner and not is_supervisor:
        flash("You are not allowed to download this attachment.")
        return redirect(url_for("dashboard"))

    return send_file(
        io.BytesIO(attachment["file_data"]),
        mimetype=attachment["content_type"] or "application/octet-stream",
        as_attachment=True,
        download_name=attachment["file_name"]
    )


@app.route("/leave/<int:request_id>/review", methods=["GET", "POST"])
@login_required
def review_leave(request_id):
    user = current_user()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            lr.*,
            e.name AS employee_name,
            e.email,
            e.department,
            e.supervisor_id
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        WHERE lr.id = %s
    """, (request_id,))

    leave = cur.fetchone()

    if not leave:
        cur.close()
        conn.close()
        flash("Leave request not found.")
        return redirect(url_for("dashboard"))

    is_admin = user["role"] == "admin"
    is_supervisor = user["employee_id"] and leave["supervisor_id"] == user["employee_id"]

    if not is_admin and not is_supervisor:
        cur.close()
        conn.close()
        flash("You are not allowed to review this request.")
        return redirect(url_for("dashboard"))

    cur.execute("""
        SELECT id, file_name, content_type, uploaded_at
        FROM leave_attachments
        WHERE leave_request_id = %s
        ORDER BY uploaded_at DESC, id DESC
    """, (request_id,))
    attachments = cur.fetchall()

    if request.method == "POST":
        action = request.form["action"]
        review_comment = request.form.get("review_comment", "")

        if action not in ["Approved", "Denied"]:
            cur.close()
            conn.close()
            flash("Invalid review action.")
            return redirect(url_for("dashboard"))

        cur.execute("""
            UPDATE leave_requests
            SET status = %s,
                reviewed_by = %s,
                reviewed_at = %s,
                review_comment = %s
            WHERE id = %s
        """, (
            action,
            user["id"],
            datetime.now(),
            review_comment,
            request_id
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash(f"Leave request {action.lower()}.")
        if user["role"] == "admin":
            return redirect(url_for("admin_leave_requests"))
        return redirect(url_for("dashboard"))

    cur.close()
    conn.close()

    return render_template("review_leave.html", leave=leave, attachments=attachments)


# ---------------- ADMIN EMPLOYEES ----------------
@app.route("/admin/employees")
@login_required
@admin_required
def admin_employees():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            e.*,
            s.name AS supervisor_name,
            u.id AS user_id,
            u.username,
            u.role
        FROM employee e
        LEFT JOIN employee s ON s.id = e.supervisor_id
        LEFT JOIN users u ON u.employee_id = e.id
        ORDER BY e.name
    """)

    employees = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("admin_employees.html", employees=employees)


@app.route("/admin/employees/add", methods=["GET", "POST"])
@login_required
@admin_required
def add_employee():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form.get("email", "").strip()
        department = request.form.get("department", "").strip()
        supervisor_id = request.form.get("supervisor_id") or None
        username = request.form["username"].strip()
        password = request.form["password"]
        role = request.form["role"]

        try:
            cur.execute("""
                INSERT INTO employee (name, email, department, supervisor_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (name, email, department, supervisor_id))

            employee_id = cur.fetchone()["id"]

            cur.execute("""
                INSERT INTO users (username, password, role, employee_id)
                VALUES (%s, %s, %s, %s)
            """, (username, password, role, employee_id))

            conn.commit()
            flash("Employee created.")

        except Exception as e:
            conn.rollback()
            flash("Error creating employee: " + str(e))

        cur.close()
        conn.close()

        return redirect(url_for("admin_employees"))

    cur.execute("""
        SELECT id, name
        FROM employee
        ORDER BY name
    """)

    supervisors = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "employee_form.html",
        emp=None,
        user_account=None,
        supervisors=supervisors,
        mode="add"
    )


@app.route("/admin/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_employee(employee_id):
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form.get("email", "").strip()
        department = request.form.get("department", "").strip()
        supervisor_id = request.form.get("supervisor_id") or None
        username = request.form["username"].strip()
        password = request.form.get("password", "")
        role = request.form["role"]

        try:
            cur.execute("""
                UPDATE employee
                SET name = %s,
                    email = %s,
                    department = %s,
                    supervisor_id = %s
                WHERE id = %s
            """, (name, email, department, supervisor_id, employee_id))

            cur.execute("""
                SELECT *
                FROM users
                WHERE employee_id = %s
            """, (employee_id,))

            existing_user = cur.fetchone()

            if existing_user:
                if password.strip():
                    cur.execute("""
                        UPDATE users
                        SET username = %s,
                            password = %s,
                            role = %s
                        WHERE employee_id = %s
                    """, (username, password, role, employee_id))
                else:
                    cur.execute("""
                        UPDATE users
                        SET username = %s,
                            role = %s
                        WHERE employee_id = %s
                    """, (username, role, employee_id))
            else:
                if not password.strip():
                    password = "password123"

                cur.execute("""
                    INSERT INTO users (username, password, role, employee_id)
                    VALUES (%s, %s, %s, %s)
                """, (username, password, role, employee_id))

            conn.commit()
            flash("Employee updated.")

        except Exception as e:
            conn.rollback()
            flash("Error updating employee: " + str(e))

        cur.close()
        conn.close()

        return redirect(url_for("admin_employees"))

    cur.execute("""
        SELECT *
        FROM employee
        WHERE id = %s
    """, (employee_id,))

    emp = cur.fetchone()

    cur.execute("""
        SELECT *
        FROM users
        WHERE employee_id = %s
    """, (employee_id,))

    user_account = cur.fetchone()

    cur.execute("""
        SELECT id, name
        FROM employee
        WHERE id <> %s
        ORDER BY name
    """, (employee_id,))

    supervisors = cur.fetchall()

    cur.close()
    conn.close()

    if not emp:
        flash("Employee not found.")
        return redirect(url_for("admin_employees"))

    return render_template(
        "employee_form.html",
        emp=emp,
        user_account=user_account,
        supervisors=supervisors,
        mode="edit"
    )


# ---------------- RUN ----------------
init_db()

if __name__ == "__main__":
    app.run()
