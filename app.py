# -*- coding: utf-8 -*-
"""
클래스매니저 — 유료 클래스 운영자(학원·공방·PT샵·쿠킹클래스)를 위한 수업 운영 도구
- 강좌 개설/관리, 수강생 관리, 수업 일정 + 출석 체크
- 수강료 정산(월별 청구·N빵 계산·입금 체크·미납자 관리), 공지사항, 대시보드
기술: Python Flask + SQLite (외부 API·유료 서비스 없음)
"""
import os
import re
import sqlite3
from datetime import date, datetime

from flask import Flask, g, render_template, request, redirect, url_for, flash, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "classmanager-demo-secret-key")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "classmanager.db"))

ATTEND_LABEL = {"present": "출석", "absent": "결석", "undecided": "미정"}
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

CATEGORIES = ["운동·레슨", "공방·핸드메이드", "쿠킹·베이킹", "음악·미술", "어학·자격증", "IT·코딩", "기타"]


# ---------------------------------------------------------------- DB
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '기타',
            description TEXT DEFAULT '',
            schedule_note TEXT DEFAULT '',
            price INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            memo TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT DEFAULT '',
            location TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'undecided',
            UNIQUE (lesson_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS fees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            month TEXT NOT NULL,
            amount INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT DEFAULT '',
            note TEXT DEFAULT '',
            UNIQUE (class_id, student_id, month)
        );
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()


with app.app_context():
    init_db()


# ---------------------------------------------------------------- helpers
def today_str():
    return date.today().isoformat()


def current_month():
    return date.today().strftime("%Y-%m")


def month_label(month):
    try:
        y, m = month.split("-")
        return f"{y}년 {int(m)}월"
    except Exception:
        return month


def dday(target_date):
    """D-day 계산: 오늘=0, 미래=+, 과거=-"""
    try:
        delta = (datetime.strptime(target_date, "%Y-%m-%d").date() - date.today()).days
        return delta
    except Exception:
        return None


def dday_text(target_date):
    d = dday(target_date)
    if d is None:
        return "-"
    if d == 0:
        return "오늘"
    if d > 0:
        return f"D-{d}"
    return f"D+{-d}"


def valid_month(value):
    return bool(value) and bool(MONTH_RE.match(value.strip()))


def parse_amount(raw):
    """금액 입력 파싱: 콤마·원 제거 후 정수. 실패 시 None"""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("원", "").replace(" ", "")
    if not s:
        return None
    if not re.fullmatch(r"\d+", s):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def get_class_or_404(class_id):
    cls = get_db().execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
    if cls is None:
        abort(404)
    return cls


def get_lesson_or_404(lesson_id):
    row = get_db().execute(
        """SELECT l.*, c.name AS class_name FROM lessons l
           JOIN classes c ON c.id = l.class_id WHERE l.id = ?""",
        (lesson_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.template_filter("won")
def won_filter(value):
    try:
        return f"{int(value):,}원"
    except (TypeError, ValueError):
        return "0원"


@app.template_filter("dday")
def dday_filter(value):
    return dday_text(value or "")


@app.template_filter("monthlabel")
def monthlabel_filter(value):
    return month_label(value or "")


@app.context_processor
def inject_globals():
    return {"today": today_str(), "this_month": current_month(), "categories": CATEGORIES}


# ---------------------------------------------------------------- 대시보드
@app.route("/dashboard")
def dashboard():
    db = get_db()
    month = current_month()
    today = today_str()

    fee = db.execute(
        """SELECT COALESCE(SUM(amount),0) AS total,
                  COALESCE(SUM(CASE WHEN paid=1 THEN amount ELSE 0 END),0) AS paid_sum,
                  COUNT(*) AS cnt,
                  COALESCE(SUM(CASE WHEN paid=1 THEN 1 ELSE 0 END),0) AS paid_cnt
           FROM fees WHERE month = ?""",
        (month,),
    ).fetchone()

    unpaid = db.execute(
        """SELECT f.id, f.amount, s.name AS student_name, c.name AS class_name
           FROM fees f
           JOIN students s ON s.id = f.student_id
           JOIN classes c ON c.id = f.class_id
           WHERE f.month = ? AND f.paid = 0
           ORDER BY f.amount DESC LIMIT 10""",
        (month,),
    ).fetchall()

    upcoming = db.execute(
        """SELECT l.*, c.name AS class_name,
                  (SELECT COUNT(*) FROM attendance a
                   JOIN students s ON s.id = a.student_id
                   WHERE a.lesson_id = l.id AND a.status='present' AND s.active=1) AS present_cnt,
                  (SELECT COUNT(*) FROM students s2
                   WHERE s2.class_id = l.class_id AND s2.active=1) AS student_cnt
           FROM lessons l JOIN classes c ON c.id = l.class_id
           WHERE l.date >= ? ORDER BY l.date ASC, l.start_time ASC LIMIT 5""",
        (today,),
    ).fetchall()

    lesson_cnt = db.execute(
        "SELECT COUNT(*) AS c FROM lessons WHERE substr(date,1,7) = ?", (month,)
    ).fetchone()["c"]
    class_cnt = db.execute("SELECT COUNT(*) AS c FROM classes").fetchone()["c"]
    student_cnt = db.execute("SELECT COUNT(*) AS c FROM students WHERE active=1").fetchone()["c"]

    notices = db.execute(
        """SELECT n.*, c.name AS class_name FROM notices n
           LEFT JOIN classes c ON c.id = n.class_id
           ORDER BY n.id DESC LIMIT 5"""
    ).fetchall()

    total = fee["total"] or 0
    rate = round((fee["paid_sum"] or 0) / total * 100) if total else 0

    return render_template(
        "dashboard.html",
        month=month,
        fee_total=total,
        fee_paid=fee["paid_sum"] or 0,
        fee_rate=rate,
        fee_cnt=fee["cnt"] or 0,
        fee_paid_cnt=fee["paid_cnt"] or 0,
        unpaid=unpaid,
        upcoming=upcoming,
        lesson_cnt=lesson_cnt,
        class_cnt=class_cnt,
        student_cnt=student_cnt,
        notices=notices,
    )


# ---------------------------------------------------------------- 강좌
@app.route("/classes")
def class_list():
    db = get_db()
    rows = db.execute(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM students s WHERE s.class_id=c.id AND s.active=1) AS student_cnt,
                  (SELECT COUNT(*) FROM lessons l WHERE l.class_id=c.id AND l.date >= ?) AS upcoming_cnt
           FROM classes c ORDER BY c.id DESC""",
        (today_str(),),
    ).fetchall()
    return render_template("classes.html", classes=rows)


@app.route("/classes/new", methods=["GET", "POST"])
def class_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "기타").strip()
        description = (request.form.get("description") or "").strip()
        schedule_note = (request.form.get("schedule_note") or "").strip()
        price = parse_amount(request.form.get("price"))
        if not name:
            flash("강좌명을 입력해주세요.", "error")
            return render_template("class_form.html", form=request.form), 400
        if category not in CATEGORIES:
            category = "기타"
        if price is None:
            price = 0
        if price < 0 or price > 100_000_000:
            flash("수강료는 0원 이상 1억원 이하로 입력해주세요.", "error")
            return render_template("class_form.html", form=request.form), 400
        try:
            db = get_db()
            cur = db.execute(
                """INSERT INTO classes (name, category, description, schedule_note, price, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (name, category, description, schedule_note, price, datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            flash(f"강좌 '{name}'이(가) 개설되었습니다.", "ok")
            return redirect(url_for("class_detail", class_id=cur.lastrowid))
        except sqlite3.Error:
            get_db().rollback()
            flash("강좌 개설 중 오류가 발생했습니다. 다시 시도해주세요.", "error")
            return render_template("class_form.html", form=request.form), 500
    return render_template("class_form.html", form={})


@app.route("/classes/<int:class_id>")
def class_detail(class_id):
    cls = get_class_or_404(class_id)
    db = get_db()
    students = db.execute(
        "SELECT * FROM students WHERE class_id=? ORDER BY active DESC, id ASC", (class_id,)
    ).fetchall()
    lessons = db.execute(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM attendance a WHERE a.lesson_id=l.id AND a.status='present') AS present_cnt,
                  (SELECT COUNT(*) FROM attendance a WHERE a.lesson_id=l.id AND a.status='absent') AS absent_cnt
           FROM lessons l WHERE l.class_id=? ORDER BY l.date DESC, l.start_time DESC LIMIT 20""",
        (class_id,),
    ).fetchall()
    notices = db.execute(
        "SELECT * FROM notices WHERE class_id=? OR class_id IS NULL ORDER BY id DESC LIMIT 5",
        (class_id,),
    ).fetchall()
    month = request.args.get("month", current_month())
    if not valid_month(month):
        month = current_month()
    fee = db.execute(
        """SELECT COALESCE(SUM(amount),0) AS total,
                  COALESCE(SUM(CASE WHEN paid=1 THEN amount ELSE 0 END),0) AS paid_sum,
                  COUNT(*) AS cnt
           FROM fees WHERE class_id=? AND month=?""",
        (class_id, month),
    ).fetchone()
    return render_template(
        "class_detail.html", cls=cls, students=students, lessons=lessons,
        notices=notices, month=month, fee=fee,
    )


@app.route("/classes/<int:class_id>/edit", methods=["GET", "POST"])
def class_edit(class_id):
    cls = get_class_or_404(class_id)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "기타").strip()
        description = (request.form.get("description") or "").strip()
        schedule_note = (request.form.get("schedule_note") or "").strip()
        price = parse_amount(request.form.get("price"))
        if not name:
            flash("강좌명을 입력해주세요.", "error")
            return render_template("class_form.html", form=request.form, cls=cls), 400
        if category not in CATEGORIES:
            category = "기타"
        if price is None:
            price = 0
        try:
            db = get_db()
            db.execute(
                "UPDATE classes SET name=?, category=?, description=?, schedule_note=?, price=? WHERE id=?",
                (name, category, description, schedule_note, price, class_id),
            )
            db.commit()
            flash("강좌 정보가 수정되었습니다.", "ok")
            return redirect(url_for("class_detail", class_id=class_id))
        except sqlite3.Error:
            get_db().rollback()
            flash("수정 중 오류가 발생했습니다.", "error")
            return render_template("class_form.html", form=request.form, cls=cls), 500
    return render_template("class_form.html", form=dict(cls), cls=cls)


@app.route("/classes/<int:class_id>/delete", methods=["POST"])
def class_delete(class_id):
    get_class_or_404(class_id)
    try:
        db = get_db()
        db.execute("DELETE FROM classes WHERE id=?", (class_id,))
        db.commit()
        flash("강좌가 삭제되었습니다.", "ok")
    except sqlite3.Error:
        get_db().rollback()
        flash("삭제 중 오류가 발생했습니다.", "error")
    return redirect(url_for("class_list"))


# ---------------------------------------------------------------- 수강생
@app.route("/classes/<int:class_id>/students/add", methods=["POST"])
def student_add(class_id):
    get_class_or_404(class_id)
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    memo = (request.form.get("memo") or "").strip()
    if not name:
        flash("수강생 이름을 입력해주세요.", "error")
        return redirect(url_for("class_detail", class_id=class_id))
    if len(name) > 30:
        flash("이름은 30자 이내로 입력해주세요.", "error")
        return redirect(url_for("class_detail", class_id=class_id))
    try:
        db = get_db()
        db.execute(
            "INSERT INTO students (class_id, name, phone, memo, joined_at) VALUES (?,?,?,?,?)",
            (class_id, name, phone, memo, datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        flash(f"수강생 '{name}'님이 등록되었습니다.", "ok")
    except sqlite3.Error:
        get_db().rollback()
        flash("등록 중 오류가 발생했습니다.", "error")
        return redirect(url_for("class_detail", class_id=class_id))
    return redirect(url_for("class_detail", class_id=class_id, celebrate=1))


@app.route("/students/<int:student_id>/toggle", methods=["POST"])
def student_toggle(student_id):
    db = get_db()
    row = db.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        db.execute("UPDATE students SET active=? WHERE id=?", (0 if row["active"] else 1, student_id))
        db.commit()
        flash(f"'{row['name']}'님을 {'휴원' if row['active'] else '복원'} 처리했습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("class_detail", class_id=row["class_id"]))


@app.route("/students/<int:student_id>/delete", methods=["POST"])
def student_delete(student_id):
    db = get_db()
    row = db.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        db.execute("DELETE FROM students WHERE id=?", (student_id,))
        db.commit()
        flash(f"'{row['name']}'님을 삭제했습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("삭제 중 오류가 발생했습니다.", "error")
    return redirect(url_for("class_detail", class_id=row["class_id"]))


# ---------------------------------------------------------------- 수업
@app.route("/lessons/new", methods=["GET", "POST"])
def lesson_new():
    db = get_db()
    classes = db.execute("SELECT * FROM classes ORDER BY id DESC").fetchall()
    if not classes:
        flash("먼저 강좌를 개설해주세요.", "error")
        return redirect(url_for("class_new"))
    if request.method == "POST":
        class_id = request.form.get("class_id")
        title = (request.form.get("title") or "").strip()
        date_ = (request.form.get("date") or "").strip()
        start_time = (request.form.get("start_time") or "").strip()
        location = (request.form.get("location") or "").strip()
        note = (request.form.get("note") or "").strip()
        try:
            class_id = int(class_id)
        except (TypeError, ValueError):
            class_id = None
        cls = db.execute("SELECT id FROM classes WHERE id=?", (class_id,)).fetchone() if class_id else None
        if cls is None:
            flash("강좌를 선택해주세요.", "error")
            return render_template("lesson_form.html", classes=classes, form=request.form), 400
        if not title:
            flash("수업 제목을 입력해주세요.", "error")
            return render_template("lesson_form.html", classes=classes, form=request.form), 400
        if not DATE_RE.match(date_):
            flash("수업 날짜를 올바르게 입력해주세요. (예: 2026-10-20)", "error")
            return render_template("lesson_form.html", classes=classes, form=request.form), 400
        if start_time and not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", start_time):
            flash("시작 시간을 HH:MM 형식으로 입력해주세요.", "error")
            return render_template("lesson_form.html", classes=classes, form=request.form), 400
        try:
            cur = db.execute(
                """INSERT INTO lessons (class_id, title, date, start_time, location, note, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (class_id, title, date_, start_time, location, note,
                 datetime.now().isoformat(timespec="seconds")),
            )
            lesson_id = cur.lastrowid
            students = db.execute(
                "SELECT id FROM students WHERE class_id=? AND active=1", (class_id,)
            ).fetchall()
            db.executemany(
                "INSERT OR IGNORE INTO attendance (lesson_id, student_id, status) VALUES (?,?,?)",
                [(lesson_id, s["id"], "undecided") for s in students],
            )
            db.commit()
            flash(f"수업 '{title}'이(가) 등록되었습니다. (출석부 {len(students)}명 생성)", "ok")
            return redirect(url_for("lesson_detail", lesson_id=lesson_id))
        except sqlite3.Error:
            db.rollback()
            flash("수업 등록 중 오류가 발생했습니다.", "error")
            return render_template("lesson_form.html", classes=classes, form=request.form), 500
    preset = request.args.get("class_id")
    return render_template("lesson_form.html", classes=classes, form={"class_id": preset} if preset else {})


@app.route("/lessons/<int:lesson_id>")
def lesson_detail(lesson_id):
    lesson = get_lesson_or_404(lesson_id)
    db = get_db()
    rows = db.execute(
        """SELECT a.id AS att_id, a.status, s.id AS student_id, s.name, s.phone, s.active
           FROM attendance a JOIN students s ON s.id = a.student_id
           WHERE a.lesson_id=? ORDER BY s.name ASC""",
        (lesson_id,),
    ).fetchall()
    counts = {"present": 0, "absent": 0, "undecided": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    return render_template("lesson_detail.html", lesson=lesson, rows=rows, counts=counts,
                           attend_label=ATTEND_LABEL)


@app.route("/lessons/<int:lesson_id>/attendance", methods=["POST"])
def attendance_update(lesson_id):
    get_lesson_or_404(lesson_id)
    student_id = request.form.get("student_id")
    status = (request.form.get("status") or "").strip()
    if status not in ATTEND_LABEL:
        flash("올바르지 않은 출석 상태입니다.", "error")
        return redirect(url_for("lesson_detail", lesson_id=lesson_id))
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        flash("수강생 정보가 올바르지 않습니다.", "error")
        return redirect(url_for("lesson_detail", lesson_id=lesson_id))
    try:
        db = get_db()
        cur = db.execute(
            "UPDATE attendance SET status=? WHERE lesson_id=? AND student_id=?",
            (status, lesson_id, student_id),
        )
        if cur.rowcount == 0:
            flash("출석부에서 해당 수강생을 찾을 수 없습니다.", "error")
        else:
            db.commit()
            flash("출석이 기록되었습니다.", "ok")
    except sqlite3.Error:
        get_db().rollback()
        flash("출석 기록 중 오류가 발생했습니다.", "error")
    return redirect(url_for("lesson_detail", lesson_id=lesson_id))


@app.route("/lessons/<int:lesson_id>/attendance/all-present", methods=["POST"])
def attendance_all_present(lesson_id):
    get_lesson_or_404(lesson_id)
    try:
        db = get_db()
        db.execute("UPDATE attendance SET status='present' WHERE lesson_id=?", (lesson_id,))
        db.commit()
        flash("전원 출석으로 처리했습니다.", "ok")
    except sqlite3.Error:
        get_db().rollback()
        flash("처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("lesson_detail", lesson_id=lesson_id))


@app.route("/lessons/<int:lesson_id>/delete", methods=["POST"])
def lesson_delete(lesson_id):
    lesson = get_lesson_or_404(lesson_id)
    try:
        db = get_db()
        db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
        db.commit()
        flash(f"수업 '{lesson['title']}'을(를) 삭제했습니다.", "ok")
    except sqlite3.Error:
        get_db().rollback()
        flash("삭제 중 오류가 발생했습니다.", "error")
    return redirect(url_for("class_detail", class_id=lesson["class_id"]))


# ---------------------------------------------------------------- 수강료 정산
@app.route("/fees")
def fee_list():
    db = get_db()
    classes = db.execute("SELECT * FROM classes ORDER BY id DESC").fetchall()
    month = (request.args.get("month") or "").strip() or current_month()
    if not valid_month(month):
        flash("월 형식이 올바르지 않아 이번 달로 표시합니다. (YYYY-MM)", "error")
        month = current_month()
    class_id = request.args.get("class_id")
    try:
        class_id = int(class_id) if class_id else None
    except (TypeError, ValueError):
        class_id = None
    if class_id is None and classes:
        class_id = classes[0]["id"]
    cls = None
    rows, total, paid_sum = [], 0, 0
    if class_id is not None:
        cls = db.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
        if cls is None:
            abort(404)
        rows = db.execute(
            """SELECT f.*, s.name AS student_name, s.phone, s.active
               FROM fees f JOIN students s ON s.id=f.student_id
               WHERE f.class_id=? AND f.month=? ORDER BY s.name ASC""",
            (class_id, month),
        ).fetchall()
        total = sum(r["amount"] for r in rows)
        paid_sum = sum(r["amount"] for r in rows if r["paid"])
    unpaid = [r for r in rows if not r["paid"]]
    rate = round(paid_sum / total * 100) if total else 0
    students = db.execute(
        "SELECT * FROM students WHERE class_id=? AND active=1 ORDER BY name ASC", (class_id,)
    ).fetchall() if class_id else []
    billed_ids = {r["student_id"] for r in rows}
    unbilled = [s for s in students if s["id"] not in billed_ids]
    return render_template(
        "fees.html", classes=classes, month=month, cls=cls, rows=rows,
        total=total, paid_sum=paid_sum, rate=rate, unpaid=unpaid, unbilled=unbilled,
    )


@app.route("/fees/bulk", methods=["POST"])
def fee_bulk():
    db = get_db()
    class_id = request.form.get("class_id")
    month = (request.form.get("month") or "").strip()
    mode = (request.form.get("mode") or "per_person").strip()
    try:
        class_id = int(class_id)
    except (TypeError, ValueError):
        flash("강좌를 선택해주세요.", "error")
        return redirect(url_for("fee_list"))
    cls = db.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if cls is None:
        abort(404)
    if not valid_month(month):
        flash("청구 월을 YYYY-MM 형식으로 입력해주세요.", "error")
        return redirect(url_for("fee_list", class_id=class_id, month=current_month()))
    students = db.execute(
        "SELECT id FROM students WHERE class_id=? AND active=1", (class_id,)
    ).fetchall()
    if not students:
        flash("청구할 수강생이 없습니다. 먼저 수강생을 등록해주세요.", "error")
        return redirect(url_for("fee_list", class_id=class_id, month=month))

    existing = {r["student_id"] for r in db.execute(
        "SELECT student_id FROM fees WHERE class_id=? AND month=?", (class_id, month)).fetchall()}
    targets = [s for s in students if s["id"] not in existing]
    if not targets:
        flash(f"{month_label(month)}에는 이미 전원에게 청구서가 있습니다.", "error")
        return redirect(url_for("fee_list", class_id=class_id, month=month))

    if mode == "total":
        total_amount = parse_amount(request.form.get("total_amount"))
        if total_amount is None or total_amount <= 0:
            flash("총액을 올바르게 입력해주세요.", "error")
            return redirect(url_for("fee_list", class_id=class_id, month=month))
        per_person = total_amount // len(targets)
        if per_person <= 0:
            flash("총액이 인원수보다 작아 1인당 금액을 계산할 수 없습니다.", "error")
            return redirect(url_for("fee_list", class_id=class_id, month=month))
        amount = per_person
        note_extra = f"총액 {total_amount:,}원 ÷ {len(targets)}명"
    else:
        amount = parse_amount(request.form.get("per_person_amount"))
        if amount is None:
            amount = cls["price"]
        if amount <= 0:
            flash("1인당 수강료를 올바르게 입력해주세요.", "error")
            return redirect(url_for("fee_list", class_id=class_id, month=month))
        note_extra = ""

    if amount > 100_000_000:
        flash("1인당 수강료가 너무 큽니다. (1억원 이하)", "error")
        return redirect(url_for("fee_list", class_id=class_id, month=month))

    try:
        db.executemany(
            """INSERT INTO fees (class_id, student_id, month, amount, note)
               VALUES (?,?,?,?,?)""",
            [(class_id, s["id"], month, amount, note_extra) for s in targets],
        )
        db.commit()
        flash(f"{month_label(month)} 수강료 청구서 {len(targets)}건을 생성했습니다. (1인당 {amount:,}원)", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("청구서 생성 중 오류가 발생했습니다.", "error")
    return redirect(url_for("fee_list", class_id=class_id, month=month))


@app.route("/fees/<int:fee_id>/toggle", methods=["POST"])
def fee_toggle(fee_id):
    db = get_db()
    row = db.execute(
        """SELECT f.*, s.name AS student_name FROM fees f
           JOIN students s ON s.id=f.student_id WHERE f.id=?""",
        (fee_id,),
    ).fetchone()
    if row is None:
        abort(404)
    try:
        paid = 0 if row["paid"] else 1
        db.execute(
            "UPDATE fees SET paid=?, paid_at=? WHERE id=?",
            (paid, datetime.now().isoformat(timespec="seconds") if paid else "", fee_id),
        )
        db.commit()
        flash(f"'{row['student_name']}'님 {row['amount']:,}원을 {'입금 확인' if paid else '미납으로 변경'}했습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("처리 중 오류가 발생했습니다.", "error")
    return redirect(url_for("fee_list", class_id=row["class_id"], month=row["month"]))


@app.route("/fees/<int:fee_id>/update", methods=["POST"])
def fee_update(fee_id):
    db = get_db()
    row = db.execute("SELECT * FROM fees WHERE id=?", (fee_id,)).fetchone()
    if row is None:
        abort(404)
    amount = parse_amount(request.form.get("amount"))
    if amount is None or amount <= 0 or amount > 100_000_000:
        flash("금액을 올바르게 입력해주세요. (1원~1억원)", "error")
        return redirect(url_for("fee_list", class_id=row["class_id"], month=row["month"]))
    try:
        db.execute("UPDATE fees SET amount=? WHERE id=?", (amount, fee_id))
        db.commit()
        flash("금액이 수정되었습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("수정 중 오류가 발생했습니다.", "error")
    return redirect(url_for("fee_list", class_id=row["class_id"], month=row["month"]))


@app.route("/fees/<int:fee_id>/delete", methods=["POST"])
def fee_delete(fee_id):
    db = get_db()
    row = db.execute("SELECT * FROM fees WHERE id=?", (fee_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        db.execute("DELETE FROM fees WHERE id=?", (fee_id,))
        db.commit()
        flash("청구서를 삭제했습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("삭제 중 오류가 발생했습니다.", "error")
    return redirect(url_for("fee_list", class_id=row["class_id"], month=row["month"]))


# ---------------------------------------------------------------- 공지사항
@app.route("/notices")
def notice_list():
    db = get_db()
    classes = db.execute("SELECT * FROM classes ORDER BY id DESC").fetchall()
    class_id = request.args.get("class_id")
    try:
        class_id = int(class_id) if class_id else None
    except (TypeError, ValueError):
        class_id = None
    if class_id is None:
        rows = db.execute(
            """SELECT n.*, c.name AS class_name FROM notices n
               LEFT JOIN classes c ON c.id=n.class_id ORDER BY n.id DESC"""
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT n.*, c.name AS class_name FROM notices n
               LEFT JOIN classes c ON c.id=n.class_id
               WHERE n.class_id=? OR n.class_id IS NULL ORDER BY n.id DESC""",
            (class_id,),
        ).fetchall()
    return render_template("notices.html", notices=rows, classes=classes, class_id=class_id)


@app.route("/notices/new", methods=["GET", "POST"])
def notice_new():
    db = get_db()
    classes = db.execute("SELECT * FROM classes ORDER BY id DESC").fetchall()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        content = (request.form.get("content") or "").strip()
        class_id = request.form.get("class_id")
        try:
            class_id = int(class_id) if class_id else None
        except (TypeError, ValueError):
            class_id = None
        if class_id is not None and not db.execute(
            "SELECT id FROM classes WHERE id=?", (class_id,)
        ).fetchone():
            class_id = None
        if not title:
            flash("제목을 입력해주세요.", "error")
            return render_template("notice_form.html", classes=classes, form=request.form), 400
        if not content:
            flash("내용을 입력해주세요.", "error")
            return render_template("notice_form.html", classes=classes, form=request.form), 400
        try:
            db.execute(
                "INSERT INTO notices (class_id, title, content, created_at) VALUES (?,?,?,?)",
                (class_id, title, content, datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            flash("공지사항이 등록되었습니다.", "ok")
            return redirect(url_for("notice_list"))
        except sqlite3.Error:
            db.rollback()
            flash("등록 중 오류가 발생했습니다.", "error")
            return render_template("notice_form.html", classes=classes, form=request.form), 500
    return render_template("notice_form.html", classes=classes, form={})


@app.route("/notices/<int:notice_id>/delete", methods=["POST"])
def notice_delete(notice_id):
    db = get_db()
    if db.execute("SELECT id FROM notices WHERE id=?", (notice_id,)).fetchone() is None:
        abort(404)
    try:
        db.execute("DELETE FROM notices WHERE id=?", (notice_id,))
        db.commit()
        flash("공지사항을 삭제했습니다.", "ok")
    except sqlite3.Error:
        db.rollback()
        flash("삭제 중 오류가 발생했습니다.", "error")
    return redirect(url_for("notice_list"))


# ---------------------------------------------------------------- 에러 페이지
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template("500.html"), 500


@app.errorhandler(Exception)
def unhandled(e):
    if isinstance(e, (KeyError,)):
        pass
    return render_template("500.html"), 500


# ================================================================
# 리트로핏 (2026-10-12): 짜증 오프닝 · 감정 해방 · 심리 · 바이럴 ·
# 감동 · 입소문 · 레거시 · 가치원칙 · 존엄
# ================================================================

# 벤치마크 (데모용 예시 수치 — 화면에 반드시 "데모 기준" 표기)
BENCHMARK = {
    "label": "같은 규모 학원·공방 평균",
    "demo_note": "데모용 예시 수치입니다",
    "collection_rate": 82,   # 월 수납률 %
    "attendance_rate": 88,   # 출석률 %
}

VALUE_BASIS = {
    "min_per_student": 2,    # 수강생 1명당 월 엑셀 정리 시간(분) — 데모 기준
    "min_per_class": 20,     # 강좌 1개당 월 정리 시간(분) — 데모 기준
    "hourly_value": 30000,   # 원장님 1시간의 가치(원) — 데모 기준
    "demo_note": "데모용 계산 기준입니다",
}


def greeting():
    """매일 루틴 — 시간대별 인사 (아침/저녁 감동 설계)"""
    h = datetime.now().hour
    if 5 <= h < 11:
        return "오늘도 힘내세요 😊"
    if 11 <= h < 17:
        return "오늘도 열심히 일하고 계시네요"
    if 17 <= h < 21:
        return "오늘 하루도 잘 보내고 계세요"
    return "오늘 수고하셨어요 🌙"


def get_setting(key, default=""):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def referral_code():
    code = get_setting("referral_code")
    if not code:
        import secrets as _secrets
        code = "WJ-" + _secrets.token_hex(3).upper()
        set_setting("referral_code", code)
    return code


def event_count(kind):
    db = get_db()
    row = db.execute("SELECT COUNT(*) AS c FROM events WHERE kind=?", (kind,)).fetchone()
    return row["c"] if row else 0


def log_event(kind):
    db = get_db()
    db.execute(
        "INSERT INTO events (kind, created_at) VALUES (?, ?)",
        (kind, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()


def prev_month(month):
    y, m = int(month[:4]), int(month[5:7])
    m -= 1
    if m == 0:
        y -= 1
        m = 12
    return f"{y}-{m:02d}"


def calc_month_value(student_count, class_count):
    """가치 계산기: 엑셀 정리 시간을 돈으로 환산 (데모 기준)"""
    minutes = student_count * VALUE_BASIS["min_per_student"] + class_count * VALUE_BASIS["min_per_class"]
    hours = minutes / 60
    saved_won = int(hours * VALUE_BASIS["hourly_value"])
    return {
        "minutes": minutes,
        "hours": round(hours, 1),
        "saved_won": saved_won,
        "saved_manwon": round(saved_won / 10000, 1),
        "year_manwon": round(saved_won * 12 / 10000),
    }


def badge_list():
    """배지 목록 — 달성 조건과 함께 반환"""
    db = get_db()
    month = current_month()
    class_cnt = db.execute("SELECT COUNT(*) c FROM classes").fetchone()["c"]
    student_cnt = db.execute("SELECT COUNT(*) c FROM students WHERE active=1").fetchone()["c"]
    lesson_cnt = db.execute("SELECT COUNT(*) c FROM lessons").fetchone()["c"]
    fee = db.execute(
        """SELECT COALESCE(SUM(amount),0) total,
                  COALESCE(SUM(CASE WHEN paid=1 THEN amount ELSE 0 END),0) paid_sum
           FROM fees WHERE month=?""",
        (month,),
    ).fetchone()
    rate = round((fee["paid_sum"] or 0) / fee["total"] * 100) if fee["total"] else 0
    referral_cnt = event_count("referral")
    export_cnt = event_count("export")
    badges = [
        {"key": "first_class", "icon": "🌱", "name": "첫 걸음",
         "desc": "첫 강좌를 개설했어요", "earned": class_cnt >= 1,
         "hint": "강좌를 1개 개설하면 받아요"},
        {"key": "students5", "icon": "👥", "name": "모임의 시작",
         "desc": "수강생 5명을 모았어요", "earned": student_cnt >= 5,
         "hint": "수강생 5명을 등록하면 받아요"},
        {"key": "full_paid", "icon": "💰", "name": "완납의 달인",
         "desc": "이번 달 수강료 100% 수납", "earned": rate == 100 and (fee["total"] or 0) > 0,
         "hint": "이번 달 수강료를 전부 받으면 받아요"},
        {"key": "lessons10", "icon": "📅", "name": "꾸준한 원장님",
         "desc": "수업 10회를 열었어요", "earned": lesson_cnt >= 10,
         "hint": "수업을 10회 등록하면 받아요"},
        {"key": "refer3", "icon": "💌", "name": "소문의 주인공",
         "desc": "원장님 3분께 추천했어요", "earned": referral_cnt >= 3,
         "hint": "추천하기를 3번 기록하면 받아요"},
        {"key": "export1", "icon": "📦", "name": "든든한 기록가",
         "desc": "내 데이터를 내려받았어요", "earned": export_cnt >= 1,
         "hint": "데이터 내려받기를 1번 하면 받아요"},
    ]
    return badges, sum(1 for b in badges if b["earned"])


# ---------------------------------------------------------------- 첫 화면: 짜증 모먼트 오프닝
@app.route("/")
def landing():
    return render_template("landing.html")


# ---------------------------------------------------------------- 원장님 오늘 브리핑
@app.route("/briefing")
def briefing():
    db = get_db()
    month = current_month()
    today = today_str()

    lessons_today = db.execute(
        """SELECT l.*, c.name AS class_name FROM lessons l
           JOIN classes c ON c.id = l.class_id
           WHERE l.date = ? ORDER BY l.start_time ASC""",
        (today,),
    ).fetchall()

    fee = db.execute(
        """SELECT COALESCE(SUM(amount),0) AS total,
                  COALESCE(SUM(CASE WHEN paid=1 THEN amount ELSE 0 END),0) AS paid_sum,
                  COALESCE(SUM(CASE WHEN paid=0 THEN 1 ELSE 0 END),0) AS unpaid_cnt
           FROM fees WHERE month = ?""",
        (month,),
    ).fetchone()
    total, paid_sum = fee["total"] or 0, fee["paid_sum"] or 0
    rate = round(paid_sum / total * 100) if total else 0

    student_cnt = db.execute("SELECT COUNT(*) c FROM students WHERE active=1").fetchone()["c"]
    new_students = db.execute(
        "SELECT COUNT(*) c FROM students WHERE substr(joined_at,1,7)=?", (month,)
    ).fetchone()["c"]
    upcoming_cnt = db.execute(
        "SELECT COUNT(*) c FROM lessons WHERE date >= ?", (today,)
    ).fetchone()["c"]

    return render_template(
        "briefing.html",
        hello=greeting(),
        month=month,
        lessons_today=lessons_today,
        fee_total=total, fee_paid=paid_sum, fee_rate=rate,
        unpaid_cnt=fee["unpaid_cnt"] or 0,
        student_cnt=student_cnt,
        new_students=new_students,
        upcoming_cnt=upcoming_cnt,
    )


# ---------------------------------------------------------------- 가치 계산기
@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    result = None
    form = {"students": "20", "classes": "3"}
    if request.method == "POST":
        try:
            students = max(0, min(10000, int((request.form.get("students") or "0").strip())))
            classes = max(0, min(1000, int((request.form.get("classes") or "0").strip())))
        except ValueError:
            flash("숫자만 입력해주세요. (예: 수강생 20명, 강좌 3개)", "error")
            return render_template("calculator.html", form=request.form, result=None,
                                   basis=VALUE_BASIS), 400
        form = {"students": str(students), "classes": str(classes)}
        result = calc_month_value(students, classes)
        result["students"] = students
        result["classes"] = classes
    return render_template("calculator.html", form=form, result=result, basis=VALUE_BASIS)


# ---------------------------------------------------------------- 월간 리포트 + 벤치마크
@app.route("/report")
def report():
    db = get_db()
    month = (request.args.get("month") or "").strip() or current_month()
    if not valid_month(month):
        month = current_month()
    pm = prev_month(month)

    def month_stats(m):
        fee = db.execute(
            """SELECT COALESCE(SUM(amount),0) total,
                      COALESCE(SUM(CASE WHEN paid=1 THEN amount ELSE 0 END),0) paid_sum
               FROM fees WHERE month=?""", (m,)).fetchone()
        att = db.execute(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END),0) present
               FROM attendance a JOIN lessons l ON l.id=a.lesson_id
               WHERE substr(l.date,1,7)=?""", (m,)).fetchone()
        return {
            "fee_total": fee["total"] or 0,
            "fee_paid": fee["paid_sum"] or 0,
            "fee_rate": round((fee["paid_sum"] or 0) / fee["total"] * 100) if fee["total"] else 0,
            "lessons": db.execute(
                "SELECT COUNT(*) c FROM lessons WHERE substr(date,1,7)=?", (m,)).fetchone()["c"],
            "att_total": att["total"] or 0,
            "att_rate": round((att["present"] or 0) / att["total"] * 100) if att["total"] else 0,
            "new_students": db.execute(
                "SELECT COUNT(*) c FROM students WHERE substr(joined_at,1,7)=?", (m,)).fetchone()["c"],
        }

    cur, prev = month_stats(month), month_stats(pm)
    return render_template("report.html", month=month, prev_month=pm,
                           cur=cur, prev=prev, benchmark=BENCHMARK)


# ---------------------------------------------------------------- 성장 시뮬레이터
@app.route("/simulator")
def simulator():
    db = get_db()
    avg_fee = db.execute(
        "SELECT COALESCE(AVG(amount),120000) a FROM fees").fetchone()["a"] or 120000
    return render_template("simulator.html", avg_fee=int(avg_fee))


# ---------------------------------------------------------------- 추천 리워드 + FOMO
@app.route("/referral", methods=["GET", "POST"])
def referral():
    if request.method == "POST":
        log_event("referral")
        flash("추천을 기록했습니다. 좋은 건 나눌수록 커져요 💌", "ok")
        return redirect(url_for("referral"))
    return render_template(
        "referral.html",
        code=referral_code(),
        count=event_count("referral"),
    )


# ---------------------------------------------------------------- 배지 + 단톡방 공유
@app.route("/badges")
def badges():
    items, earned = badge_list()
    return render_template("badges.html", badges=items, earned=earned, total=len(items))


# ---------------------------------------------------------------- 우리의 약속 (레거시 철학 + 가치 7원칙)
@app.route("/about")
def about():
    return render_template("about.html")


# ---------------------------------------------------------------- 가게 이어받기: 데이터 내려받기
@app.route("/export")
def export_data():
    db = get_db()
    data = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "notice": "이 데이터는 10년 후에도 원장님의 것입니다.",
        "classes": [dict(r) for r in db.execute("SELECT * FROM classes").fetchall()],
        "students": [dict(r) for r in db.execute("SELECT * FROM students").fetchall()],
        "lessons": [dict(r) for r in db.execute("SELECT * FROM lessons").fetchall()],
        "attendance": [dict(r) for r in db.execute("SELECT * FROM attendance").fetchall()],
        "fees": [dict(r) for r in db.execute("SELECT * FROM fees").fetchall()],
        "notices": [dict(r) for r in db.execute("SELECT * FROM notices").fetchall()],
    }
    log_event("export")
    from flask import Response
    import json as _json
    body = _json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=classmanager-backup.json"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    app.run(host="127.0.0.1", port=port, debug=False)
