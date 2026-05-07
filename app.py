from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import os
import io
import csv
import zipfile
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape
import psycopg2
from psycopg2.extras import RealDictCursor
from functools import wraps
from datetime import datetime, date, timedelta
import calendar
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key")

DEPARTMENTS = ["Sales", "Marketing", "Office", "Warehouse"]
EMPLOYEE_STATUSES = ["Active", "Inactive"]


def normalise_department(department):
    department = (department or "").strip()
    for valid_department in DEPARTMENTS:
        if department.lower() == valid_department.lower():
            return valid_department
    return ""


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
    cur.execute("ALTER TABLE employee ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Active'")
    cur.execute("UPDATE employee SET status = 'Active' WHERE status IS NULL OR TRIM(status) = ''")
    cur.execute("UPDATE employee SET status = 'Active' WHERE LOWER(TRIM(status)) = 'active'")
    cur.execute("UPDATE employee SET status = 'Inactive' WHERE LOWER(TRIM(status)) = 'inactive'")

    cur.execute("UPDATE employee SET department = 'Sales' WHERE LOWER(TRIM(COALESCE(department, ''))) = 'sales'")
    cur.execute("UPDATE employee SET department = 'Marketing' WHERE LOWER(TRIM(COALESCE(department, ''))) = 'marketing'")
    cur.execute("UPDATE employee SET department = 'Office' WHERE LOWER(TRIM(COALESCE(department, ''))) = 'office'")
    cur.execute("UPDATE employee SET department = 'Warehouse' WHERE LOWER(TRIM(COALESCE(department, ''))) = 'warehouse'")

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


# ---------------- LEAVE HELPERS ----------------
def validate_leave_dates(start_date, end_date, start_time, end_time):
    if not start_date or not end_date:
        return "Start date and finish date are required."

    if end_date < start_date:
        return "Finish date cannot be before start date."

    if not start_time or not end_time:
        return "Start time and finish time are required."

    if start_date == end_date and end_time <= start_time:
        return "Finish time must be after start time when leave is on the same day."

    return None


def save_leave_attachment(cur, leave_request_id, attachment):
    if not attachment or not attachment.filename:
        return False

    file_name = secure_filename(attachment.filename)
    if not file_name:
        return False

    file_data = attachment.read()
    if not file_data:
        return False

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

    return True



# ---------------- EMAIL HELPERS ----------------
def email_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM_EMAIL"))


def smtp_use_tls():
    return os.environ.get("SMTP_USE_TLS", "true").strip().lower() not in ["0", "false", "no", "off"]


def build_absolute_url(endpoint, **values):
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")

    if base_url:
        relative_url = url_for(endpoint, **values)
        return base_url + relative_url

    return url_for(endpoint, _external=True, **values)


def email_enabled():
    return os.environ.get("EMAIL_ENABLED", "true").strip().lower() not in ["0", "false", "no", "off"]


def smtp_timeout_seconds():
    try:
        timeout = int(os.environ.get("SMTP_TIMEOUT", "5"))
    except Exception:
        timeout = 5

    if timeout < 1:
        timeout = 1

    if timeout > 10:
        timeout = 10

    return timeout


def _send_html_email_now(recipients, subject, html_body):
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", "").strip()
    from_name = os.environ.get("SMTP_FROM_NAME", "HR Leave System").strip()
    timeout = smtp_timeout_seconds()

    if not smtp_host or not from_email:
        print("Email skipped: SMTP_HOST or SMTP_FROM_EMAIL is missing.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    try:
        print(f"Sending email via {smtp_host}:{smtp_port}, timeout={timeout}s, recipients={len(recipients)}")

        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as smtp:
            if smtp_use_tls():
                smtp.starttls()

            if smtp_username:
                smtp.login(smtp_username, smtp_password)

            smtp.sendmail(from_email, recipients, msg.as_string())

        print("Email sent successfully.")
        return True

    except Exception as e:
        print("Email send failed: " + repr(e))
        return False


def send_html_email(to_addresses, subject, html_body):
    if not email_enabled():
        print("Email skipped: EMAIL_ENABLED is false.")
        return False

    if isinstance(to_addresses, str):
        to_addresses = [to_addresses]

    recipients = []
    for addr in to_addresses or []:
        addr = (addr or "").strip()
        if addr and addr not in recipients:
            recipients.append(addr)

    if not recipients:
        print("Email skipped: no recipients.")
        return False

    async_email = os.environ.get("EMAIL_ASYNC", "true").strip().lower() not in ["0", "false", "no", "off"]

    if async_email:
        thread = threading.Thread(
            target=_send_html_email_now,
            args=(recipients, subject, html_body),
            daemon=True
        )
        thread.start()
        print("Email queued in background thread.")
        return True

    return _send_html_email_now(recipients, subject, html_body)


def get_leave_email_context(cur, leave_request_id):
    cur.execute("""
        SELECT
            lr.*,
            e.name AS employee_name,
            e.email AS employee_email,
            e.department,
            s.name AS supervisor_name,
            s.email AS supervisor_email,
            reviewer.username AS reviewer_name,
            (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        LEFT JOIN employee s ON s.id = e.supervisor_id
        LEFT JOIN users reviewer ON reviewer.id = lr.reviewed_by
        WHERE lr.id = %s
    """, (leave_request_id,))

    return cur.fetchone()


def format_leave_email_body(leave, heading, message):
    view_link = build_absolute_url("view_leave", request_id=leave["id"])

    attachment_text = "Yes" if leave.get("attachment_count", 0) else "No"

    return f"""
        <html>
        <body style="font-family: Arial, sans-serif; font-size: 14px; color: #222;">
            <h2>{escape(heading)}</h2>
            <p>{escape(message)}</p>

            <table cellpadding="6" cellspacing="0" border="1" style="border-collapse: collapse;">
                <tr><th align="left">Leave ID</th><td>{escape(str(leave.get('id', '')))}</td></tr>
                <tr><th align="left">Employee</th><td>{escape(str(leave.get('employee_name') or ''))}</td></tr>
                <tr><th align="left">Department</th><td>{escape(str(leave.get('department') or ''))}</td></tr>
                <tr><th align="left">Leave Type</th><td>{escape(str(leave.get('leave_type') or ''))}</td></tr>
                <tr><th align="left">Start</th><td>{escape(str(leave.get('start_date') or ''))} {escape(str(leave.get('start_time') or ''))}</td></tr>
                <tr><th align="left">Finish</th><td>{escape(str(leave.get('end_date') or ''))} {escape(str(leave.get('end_time') or ''))}</td></tr>
                <tr><th align="left">Reason</th><td>{escape(str(leave.get('reason') or ''))}</td></tr>
                <tr><th align="left">Attachment</th><td>{attachment_text}</td></tr>
                <tr><th align="left">Status</th><td>{escape(str(leave.get('status') or ''))}</td></tr>
                <tr><th align="left">Review Comment</th><td>{escape(str(leave.get('review_comment') or ''))}</td></tr>
            </table>

            <p><a href="{escape(view_link)}">Open leave request</a></p>
        </body>
        </html>
    """


def supervisor_recipients(leave):
    recipients = []

    if leave.get("supervisor_email"):
        recipients.append(leave["supervisor_email"])

    fallback_email = os.environ.get("HR_ADMIN_EMAIL", "").strip()
    if not recipients and fallback_email:
        recipients.append(fallback_email)

    return recipients


def notify_leave_submitted(leave):
    subject = f"Leave request submitted - {leave.get('employee_name', '')} - #{leave.get('id')}"
    body = format_leave_email_body(
        leave,
        "Leave Request Submitted",
        "A leave request has been submitted and is waiting for approval."
    )
    return send_html_email(supervisor_recipients(leave), subject, body)


def notify_leave_updated(leave, was_approved):
    subject = f"Leave request updated - {leave.get('employee_name', '')} - #{leave.get('id')}"

    if was_approved:
        message = "An approved leave request has been edited. It has been moved back to Pending and needs approval again."
    else:
        message = "A leave request has been updated and is waiting for approval."

    body = format_leave_email_body(leave, "Leave Request Updated", message)
    return send_html_email(supervisor_recipients(leave), subject, body)


def notify_leave_cancelled(leave):
    subject = f"Leave request cancelled - {leave.get('employee_name', '')} - #{leave.get('id')}"
    body = format_leave_email_body(
        leave,
        "Leave Request Cancelled",
        "A leave request has been cancelled."
    )
    return send_html_email(supervisor_recipients(leave), subject, body)


def notify_leave_reviewed(leave):
    subject = f"Leave request {str(leave.get('status', '')).lower()} - #{leave.get('id')}"
    body = format_leave_email_body(
        leave,
        f"Leave Request {leave.get('status', '')}",
        "Your leave request has been reviewed."
    )
    return send_html_email(leave.get("employee_email"), subject, body)
def build_admin_leave_request_query(filter_start, filter_end, filter_status, filter_employee_id=None, include_attachment_list=False):
    attachment_select = """
            (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count
    """

    if include_attachment_list:
        attachment_select += """,
            (
                SELECT STRING_AGG(la.file_name, ', ' ORDER BY la.uploaded_at DESC, la.id DESC)
                FROM leave_attachments la
                WHERE la.leave_request_id = lr.id
            ) AS attachment_files
        """

    sql = f"""
        SELECT
            lr.*,
            e.name AS employee_name,
            e.email AS employee_email,
            e.department,
            reviewer.username AS reviewer_name,
            {attachment_select}
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

    if filter_employee_id:
        sql += " AND e.id = %s "
        params.append(filter_employee_id)

    sql += """
        ORDER BY lr.start_date DESC, lr.submitted_at DESC
    """

    return sql, params


def build_employee_leave_request_query(employee_id, filter_start, filter_end, filter_status, filter_employee_id=None):
    sql = """
        SELECT
            lr.*,
            e.name AS employee_name,
            e.email AS employee_email,
            e.department,
            reviewer.username AS reviewer_name,
            (SELECT COUNT(*) FROM leave_attachments la WHERE la.leave_request_id = lr.id) AS attachment_count,
            CASE
                WHEN lr.employee_id = %s THEN 'Mine'
                ELSE 'Supervised'
            END AS request_scope
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        LEFT JOIN users reviewer ON reviewer.id = lr.reviewed_by
        WHERE (lr.employee_id = %s OR e.supervisor_id = %s)
    """

    params = [employee_id, employee_id, employee_id]

    if filter_start:
        sql += " AND lr.end_date >= %s "
        params.append(filter_start)

    if filter_end:
        sql += " AND lr.start_date <= %s "
        params.append(filter_end)

    if filter_status:
        sql += " AND lr.status = %s "
        params.append(filter_status)

    if filter_employee_id:
        sql += " AND e.id = %s "
        params.append(filter_employee_id)

    sql += """
        ORDER BY lr.start_date DESC, lr.submitted_at DESC
    """

    return sql, params


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
            e.supervisor_id,
            COALESCE(e.status, 'Active') AS employee_status
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
            SELECT
                u.*,
                COALESCE(e.status, 'Active') AS employee_status
            FROM users u
            LEFT JOIN employee e ON e.id = u.employee_id
            WHERE u.username = %s
              AND u.password = %s
        """, (username, password))

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and user["employee_id"] and user.get("employee_status") == "Inactive":
            flash("Your account is inactive. Please contact HR or your manager.")
            return render_template("login.html")

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



# ---------------- LEAVE CALENDAR ----------------
@app.route("/calendar")
@login_required
def leave_calendar():
    user = current_user()

    today = date.today()

    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except Exception:
        year = today.year
        month = today.month

    if month < 1 or month > 12:
        year = today.year
        month = today.month

    filter_status = request.args.get("status", "Active").strip()

    first_day = date(year, month, 1)
    _, last_day_num = calendar.monthrange(year, month)
    last_day = date(year, month, last_day_num)

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    # Calendar grid starts Monday and ends Sunday.
    grid_start = first_day - timedelta(days=first_day.weekday())
    grid_end = last_day + timedelta(days=(6 - last_day.weekday()))

    conn = get_db()
    cur = conn.cursor()

    sql = """
        SELECT
            lr.id,
            lr.employee_id,
            lr.leave_type,
            lr.start_date,
            lr.end_date,
            lr.start_time,
            lr.end_time,
            lr.status,
            lr.reason,
            e.name AS employee_name,
            e.department,
            CASE WHEN lr.employee_id = %s THEN 1 ELSE 0 END AS is_mine
        FROM leave_requests lr
        JOIN employee e ON e.id = lr.employee_id
        WHERE lr.start_date <= %s
          AND lr.end_date >= %s
    """

    params = [user["employee_id"], grid_end, grid_start]

    if user["role"] != "admin":
        if not user["employee_id"]:
            cur.close()
            conn.close()
            flash("Calendar is only available to employee-linked accounts.")
            return redirect(url_for("dashboard"))

        sql += """
          AND (
                lr.employee_id = %s
                OR (
                    COALESCE(e.department, '') <> ''
                    AND e.department = %s
                )
          )
        """
        params.extend([user["employee_id"], user["department"] or ""])

    if filter_status == "Active" or not filter_status:
        sql += " AND lr.status IN ('Pending', 'Approved') "
    elif filter_status != "All":
        sql += " AND lr.status = %s "
        params.append(filter_status)

    sql += " ORDER BY lr.start_date, e.name, lr.leave_type "

    cur.execute(sql, params)
    leave_rows = cur.fetchall()

    cur.close()
    conn.close()

    days = []
    current_day = grid_start
    while current_day <= grid_end:
        days.append(current_day)
        current_day += timedelta(days=1)

    events_by_day = {}
    for d in days:
        day_events = []
        for r in leave_rows:
            if r["start_date"] <= d <= r["end_date"]:
                day_events.append(r)
        events_by_day[d.isoformat()] = day_events

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_name=calendar.month_name[month],
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        days=days,
        events_by_day=events_by_day,
        today=today,
        filter_status=filter_status,
        user=user
    )


# ---------------- EMPLOYEE LEAVE REQUESTS ----------------
@app.route("/my/leave-requests")
@login_required
def employee_leave_requests():
    user = current_user()

    if not user["employee_id"]:
        flash("Admin account is not linked to an employee.")
        return redirect(url_for("dashboard"))

    filter_start = request.args.get("start_date", "").strip()
    filter_end = request.args.get("end_date", "").strip()
    filter_status = request.args.get("status", "").strip()
    filter_employee_id = request.args.get("employee_id", "").strip()

    conn = get_db()
    cur = conn.cursor()

    sql, params = build_employee_leave_request_query(
        user["employee_id"],
        filter_start,
        filter_end,
        filter_status,
        filter_employee_id
    )

    cur.execute(sql, params)
    leave_requests = cur.fetchall()

    cur.execute("""
        SELECT id, name
        FROM employee
        WHERE id = %s OR supervisor_id = %s
        ORDER BY name
    """, (user["employee_id"], user["employee_id"]))
    employees = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "employee_leave_requests.html",
        leave_requests=leave_requests,
        employees=employees,
        filter_start=filter_start,
        filter_end=filter_end,
        filter_status=filter_status,
        filter_employee_id=filter_employee_id
    )


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        conn = get_db()
        cur = conn.cursor()

        try:
            if user["employee_id"]:
                cur.execute("""
                    UPDATE employee
                    SET email = %s
                    WHERE id = %s
                """, (email, user["employee_id"]))

            if new_password or confirm_password or current_password:
                if not current_password:
                    raise Exception("Current password is required to change password.")

                if new_password != confirm_password:
                    raise Exception("New password and confirm password do not match.")

                if len(new_password) < 6:
                    raise Exception("New password must be at least 6 characters.")

                cur.execute("""
                    SELECT password
                    FROM users
                    WHERE id = %s
                """, (user["id"],))

                existing = cur.fetchone()

                if not existing or existing["password"] != current_password:
                    raise Exception("Current password is incorrect.")

                cur.execute("""
                    UPDATE users
                    SET password = %s
                    WHERE id = %s
                """, (new_password, user["id"]))

            conn.commit()
            flash("Profile updated.")

        except Exception as e:
            conn.rollback()
            flash("Error updating profile: " + str(e))

        cur.close()
        conn.close()

        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


# ---------------- ADMIN LEAVE REQUESTS ----------------
@app.route("/admin/leave-requests")
@login_required
@admin_required
def admin_leave_requests():
    filter_start = request.args.get("start_date", "").strip()
    filter_end = request.args.get("end_date", "").strip()
    filter_status = request.args.get("status", "").strip()
    filter_employee_id = request.args.get("employee_id", "").strip()

    conn = get_db()
    cur = conn.cursor()

    sql, params = build_admin_leave_request_query(
        filter_start,
        filter_end,
        filter_status,
        filter_employee_id
    )

    cur.execute(sql, params)
    all_requests = cur.fetchall()

    cur.execute("""
        SELECT id, name
        FROM employee
        ORDER BY name
    """)
    employees = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "admin_leave_requests.html",
        all_requests=all_requests,
        employees=employees,
        filter_start=filter_start,
        filter_end=filter_end,
        filter_status=filter_status,
        filter_employee_id=filter_employee_id
    )


@app.route("/admin/leave-requests/export")
@login_required
@admin_required
def export_admin_leave_requests():
    filter_start = request.args.get("start_date", "").strip()
    filter_end = request.args.get("end_date", "").strip()
    filter_status = request.args.get("status", "").strip()
    filter_employee_id = request.args.get("employee_id", "").strip()

    conn = get_db()
    cur = conn.cursor()

    sql, params = build_admin_leave_request_query(
        filter_start,
        filter_end,
        filter_status,
        filter_employee_id,
        include_attachment_list=True
    )

    cur.execute(sql, params)
    all_requests = cur.fetchall()

    leave_ids = [r["id"] for r in all_requests]
    attachments = []

    if leave_ids:
        cur.execute("""
            SELECT
                la.id,
                la.leave_request_id,
                la.file_name,
                la.content_type,
                la.file_data,
                la.uploaded_at
            FROM leave_attachments la
            WHERE la.leave_request_id = ANY(%s)
            ORDER BY la.leave_request_id, la.uploaded_at DESC, la.id DESC
        """, (leave_ids,))
        attachments = cur.fetchall()

    cur.close()
    conn.close()

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)

    writer.writerow([
        "ID",
        "Employee",
        "Employee Email",
        "Department",
        "Leave Type",
        "Start Date",
        "Start Time",
        "Finish Date",
        "Finish Time",
        "Reason",
        "Attachment Count",
        "Attachment Files",
        "Status",
        "Submitted At",
        "Reviewed By",
        "Reviewed At",
        "Review Comment"
    ])

    for r in all_requests:
        writer.writerow([
            r.get("id", ""),
            r.get("employee_name", ""),
            r.get("employee_email", ""),
            r.get("department", ""),
            r.get("leave_type", ""),
            r.get("start_date", ""),
            r.get("start_time", ""),
            r.get("end_date", ""),
            r.get("end_time", ""),
            r.get("reason", ""),
            r.get("attachment_count", 0),
            r.get("attachment_files", "") or "",
            r.get("status", ""),
            r.get("submitted_at", ""),
            r.get("reviewer_name", ""),
            r.get("reviewed_at", ""),
            r.get("review_comment", "")
        ])

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("leave_requests.csv", csv_buffer.getvalue())

        for a in attachments:
            safe_name = secure_filename(a["file_name"]) or f"attachment_{a['id']}"
            zip_path = f"attachments/leave_{a['leave_request_id']}/{a['id']}_{safe_name}"
            file_data = a["file_data"]
            if isinstance(file_data, memoryview):
                file_data = file_data.tobytes()
            zf.writestr(zip_path, bytes(file_data))

    zip_buffer.seek(0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"leave_requests_export_{stamp}.zip"
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

        validation_error = validate_leave_dates(start_date, end_date, start_time, end_time)
        if validation_error:
            flash(validation_error)
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

            save_leave_attachment(cur, leave_request_id, attachment)

            leave_for_email = get_leave_email_context(cur, leave_request_id)

            conn.commit()
            notify_leave_submitted(leave_for_email)
            flash("Leave request submitted.")

        except Exception as e:
            conn.rollback()
            flash("Error submitting leave request: " + str(e))

        cur.close()
        conn.close()

        return redirect(url_for("dashboard"))

    return render_template("leave_form.html", leave=None, attachments=[], mode="new")


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
        can_review=(is_admin or is_supervisor),
        can_edit=(is_admin or is_owner)
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


@app.route("/leave/<int:request_id>/edit", methods=["GET", "POST"])
@login_required
def edit_leave(request_id):
    user = current_user()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            lr.*,
            e.name AS employee_name,
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

    is_owner = user["employee_id"] and leave["employee_id"] == user["employee_id"]
    is_admin = user["role"] == "admin"

    if not is_owner and not is_admin:
        cur.close()
        conn.close()
        flash("You are not allowed to edit this request.")
        return redirect(url_for("dashboard"))

    if leave["status"] == "Cancelled":
        cur.close()
        conn.close()
        flash("Cancelled leave requests cannot be edited. Please submit a new leave request.")
        return redirect(url_for("view_leave", request_id=request_id))

    if request.method == "POST":
        leave_type = request.form["leave_type"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        reason = request.form.get("reason", "")
        attachment = request.files.get("attachment")
        remove_attachment_ids = request.form.getlist("remove_attachment_ids")

        validation_error = validate_leave_dates(start_date, end_date, start_time, end_time)
        if validation_error:
            flash(validation_error)
            return redirect(url_for("edit_leave", request_id=request_id))

        try:
            cur.execute("""
                UPDATE leave_requests
                SET leave_type = %s,
                    start_date = %s,
                    end_date = %s,
                    start_time = %s,
                    end_time = %s,
                    reason = %s,
                    status = 'Pending',
                    reviewed_by = NULL,
                    reviewed_at = NULL,
                    review_comment = NULL
                WHERE id = %s
            """, (
                leave_type,
                start_date,
                end_date,
                start_time,
                end_time,
                reason,
                request_id
            ))

            for attachment_id in remove_attachment_ids:
                cur.execute("""
                    DELETE FROM leave_attachments
                    WHERE id = %s
                      AND leave_request_id = %s
                """, (attachment_id, request_id))

            save_leave_attachment(cur, request_id, attachment)

            leave_for_email = get_leave_email_context(cur, request_id)
            was_approved = leave["status"] == "Approved"

            conn.commit()
            notify_leave_updated(leave_for_email, was_approved)

            if was_approved:
                flash("Leave request updated. It has been moved back to Pending and needs supervisor approval again.")
            else:
                flash("Leave request updated.")

        except Exception as e:
            conn.rollback()
            flash("Error updating leave request: " + str(e))

        cur.close()
        conn.close()
        return redirect(url_for("view_leave", request_id=request_id))

    cur.execute("""
        SELECT id, file_name, content_type, uploaded_at
        FROM leave_attachments
        WHERE leave_request_id = %s
        ORDER BY uploaded_at DESC, id DESC
    """, (request_id,))
    attachments = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("leave_form.html", leave=leave, attachments=attachments, mode="edit")


@app.route("/leave/<int:request_id>/cancel", methods=["POST"])
@login_required
def cancel_leave(request_id):
    user = current_user()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT employee_id, status
        FROM leave_requests
        WHERE id = %s
    """, (request_id,))

    leave = cur.fetchone()

    if not leave:
        cur.close()
        conn.close()
        flash("Leave request not found.")
        return redirect(url_for("dashboard"))

    is_owner = user["employee_id"] and leave["employee_id"] == user["employee_id"]
    is_admin = user["role"] == "admin"

    if not is_owner and not is_admin:
        cur.close()
        conn.close()
        flash("You are not allowed to cancel this request.")
        return redirect(url_for("dashboard"))

    if leave["status"] == "Cancelled":
        cur.close()
        conn.close()
        flash("Leave request is already cancelled.")
        return redirect(url_for("view_leave", request_id=request_id))

    cur.execute("""
        UPDATE leave_requests
        SET status = 'Cancelled',
            reviewed_by = NULL,
            reviewed_at = NULL,
            review_comment = 'Cancelled by employee.'
        WHERE id = %s
    """, (request_id,))

    leave_for_email = get_leave_email_context(cur, request_id)

    conn.commit()
    notify_leave_cancelled(leave_for_email)
    cur.close()
    conn.close()

    flash("Leave request cancelled.")
    return redirect(url_for("dashboard"))


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

    if leave["status"] != "Pending":
        cur.close()
        conn.close()
        flash("Only pending leave requests can be reviewed.")
        return redirect(url_for("view_leave", request_id=request_id))

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

        leave_for_email = get_leave_email_context(cur, request_id)

        conn.commit()
        notify_leave_reviewed(leave_for_email)
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
            COALESCE(e.status, 'Active') AS employee_status,
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
        department = normalise_department(request.form.get("department", ""))
        supervisor_id = request.form.get("supervisor_id") or None
        username = request.form["username"].strip()
        password = request.form["password"]
        role = request.form["role"]
        employee_status = request.form.get("employee_status", "Active")

        if employee_status not in EMPLOYEE_STATUSES:
            employee_status = "Active"

        if department not in DEPARTMENTS:
            flash("Please select a valid department.")
            cur.close()
            conn.close()
            return redirect(url_for("add_employee"))

        try:
            cur.execute("""
                INSERT INTO employee (name, email, department, supervisor_id, status)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (name, email, department, supervisor_id, employee_status))

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
        WHERE COALESCE(status, 'Active') = 'Active'
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
        departments=DEPARTMENTS,
        employee_statuses=EMPLOYEE_STATUSES,
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
        department = normalise_department(request.form.get("department", ""))
        supervisor_id = request.form.get("supervisor_id") or None
        username = request.form["username"].strip()
        password = request.form.get("password", "")
        role = request.form["role"]
        employee_status = request.form.get("employee_status", "Active")

        if employee_status not in EMPLOYEE_STATUSES:
            employee_status = "Active"

        if department not in DEPARTMENTS:
            flash("Please select a valid department.")
            cur.close()
            conn.close()
            return redirect(url_for("edit_employee", employee_id=employee_id))

        try:
            cur.execute("""
                UPDATE employee
                SET name = %s,
                    email = %s,
                    department = %s,
                    supervisor_id = %s,
                    status = %s
                WHERE id = %s
            """, (name, email, department, supervisor_id, employee_status, employee_id))

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
          AND COALESCE(status, 'Active') = 'Active'
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
        departments=DEPARTMENTS,
        employee_statuses=EMPLOYEE_STATUSES,
        mode="edit"
    )


# ---------------- RUN ----------------
init_db()

if __name__ == "__main__":
    app.run()
