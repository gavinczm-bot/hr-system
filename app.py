from flask import Flask, render_template, request, redirect, url_for, session
import os
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key")


# ---------------- DB ----------------
def get_db():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is missing.")

    conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # User table
    # Use "users" instead of "user" because user can cause issues in PostgreSQL
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    # Employee table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee (
            id SERIAL PRIMARY KEY,
            name TEXT,
            department TEXT,
            salary NUMERIC
        )
    """)

    # Default admin user
    # This will also reset admin password back to paper1234 if admin already exists
    cur.execute("""
        INSERT INTO users (username, password)
        VALUES (%s, %s)
        ON CONFLICT (username)
        DO UPDATE SET password = EXCLUDED.password
    """, ("admin", "paper1234"))

    conn.commit()
    cur.close()
    conn.close()


# ---------------- AUTH ----------------
def login_required(func):
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM users WHERE username = %s AND password = %s",
            (username, password)
        )

        user = cur.fetchone()

        cur.close()
        conn.close()

        print("Input:", username, password)
        print("DB result:", user)

        if user:
            session["user"] = username
            return redirect(url_for("index"))
        else:
            return "Login Failed"

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ---------------- ROUTES ----------------
@app.route("/")
@login_required
def index():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM employee
        ORDER BY id
    """)

    employees = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("index.html", employees=employees)


@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "POST":
        name = request.form["name"]
        dept = request.form["department"]
        salary = request.form["salary"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO employee (name, department, salary)
            VALUES (%s, %s, %s)
            """,
            (name, dept, salary)
        )

        conn.commit()
        cur.close()
        conn.close()

        return redirect(url_for("index"))

    return render_template("form.html", emp=None)


@app.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit(id):
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"]
        dept = request.form["department"]
        salary = request.form["salary"]

        cur.execute(
            """
            UPDATE employee
            SET name = %s,
                department = %s,
                salary = %s
            WHERE id = %s
            """,
            (name, dept, salary, id)
        )

        conn.commit()
        cur.close()
        conn.close()

        return redirect(url_for("index"))

    cur.execute(
        "SELECT * FROM employee WHERE id = %s",
        (id,)
    )

    emp = cur.fetchone()

    cur.close()
    conn.close()

    return render_template("form.html", emp=emp)


@app.route("/delete/<int:id>")
@login_required
def delete(id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM employee WHERE id = %s",
        (id,)
    )

    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for("index"))


# ---------------- RUN ----------------
# Important for Render / gunicorn:
# This runs when app.py is imported, so tables are created even when using gunicorn app:app
init_db()

if __name__ == "__main__":
    app.run()