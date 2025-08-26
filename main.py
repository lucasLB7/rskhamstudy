from flask import Flask, render_template, request, redirect, url_for, session, abort, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from pathlib import Path
import random, os
from models import db, User, Category, Question

app = Flask(__name__, instance_relative_config=True)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

# ensure instance/ exists
Path(app.instance_path).mkdir(parents=True, exist_ok=True)

# keep JSON sorting behaviour
app.config['JSON_SORT_KEYS'] = False
try:
    app.json.sort_keys = False
except Exception:
    pass

# (optional) make template edits auto-refresh
app.config["TEMPLATES_AUTO_RELOAD"] = True

# SQLite: instance/rskhamstudy.db
db_path = Path(app.instance_path) / "rskhamstudy.db"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
from flask_migrate import Migrate
migrate = Migrate(app, db)

# --- Auth setup ---
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id: str):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

# Create tables + seed categories + seed admin once
with app.app_context():
    db.create_all()
    if not Category.query.first():
        for name in ["Foundation", "Intermediary", "Full licence", "Unassigned"]:
            db.session.add(Category(name=name))
        db.session.commit()
    if not User.query.filter_by(is_admin=True).first():
        admin = User(email="admin@local", name="Admin", is_admin=True, level=None)
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "changeme"))
        db.session.add(admin)
        db.session.commit()
        print("Seeded admin: admin@local / changeme (change ASAP)")

# ---------- Quiz helpers ----------
def _question_to_dict(q: Question):
    texts = [q.choice_a, q.choice_b, q.choice_c, q.choice_d]
    letters = ["A", "B", "C", "D"]
    pairs = list(zip(letters, texts))
    correct_letter = q.correct_answer
    correct_text = {"A": q.choice_a, "B": q.choice_b, "C": q.choice_c, "D": q.choice_d}[correct_letter]
    return {
        "id": q.id,
        "question": q.text,
        "pairs": pairs,
        "correct_letter": correct_letter,
        "correct_text": correct_text,
    }

def _build_pool(cat_id, seed, cap_count):
    base = Question.query
    if cat_id:
        base = base.filter_by(category_id=cat_id)
    ids = [qid for (qid,) in base.with_entities(Question.id).order_by(Question.id.asc()).all()]
    rnd = random.Random(seed)
    rnd.shuffle(ids)
    return ids[:cap_count]

def _clear_quiz_session():
    """Remove only quiz-related keys; keep Flask-Login session intact."""
    for k in [
        'mode','cat_id','seed','cursor','total_in_session',
        'correct','wrong','wrong_ids','last_choice','last_feedback',
        'study_queue','review_only'
    ]:
        session.pop(k, None)

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template("index.html")

# Auth ---------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid credentials", "danger")
            return render_template("login.html")

        login_user(user)
        flash("Welcome back!", "success")

        # Prefer ?next=... if present; otherwise admins to /admin, users to /
        next_url = request.args.get('next')
        if not next_url:
            next_url = url_for('admin_home') if user.is_admin else url_for('index')
        return redirect(next_url)

    return render_template("login.html")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Signed out", "info")
    return redirect(url_for('index'))

# Admin decorator
from functools import wraps
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            # redirect unauthenticated users to login with ?next=
            return redirect(url_for('login', next=request.path))
        if not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

# Start quiz -----
@app.route('/start/<mode>')
def start(mode):
    """Optionally pass ?cat=Foundation. If user has a level and no ?cat given, use their level."""
    cat_name = request.args.get("cat")
    if not cat_name and current_user.is_authenticated and current_user.level:
        cat_name = current_user.level

    cat_id = None
    if cat_name:
        cat = Category.query.filter_by(name=cat_name).first()
        if not cat:
            abort(404)
        cat_id = cat.id

    base = Question.query
    if cat_id:
        base = base.filter_by(category_id=cat_id)
    total_available = base.count()
    cap = min(60, total_available) if mode == "exam" else total_available

    # scoped clear: DO NOT session.clear() (keeps you logged in)
    _clear_quiz_session()

    session['mode'] = mode
    session['cat_id'] = cat_id
    session['seed'] = random.randrange(2**31)
    session['cursor'] = 0
    session['total_in_session'] = cap
    session['correct'] = 0
    session['wrong'] = 0
    session['wrong_ids'] = []
    session['last_choice'] = {}
    session['last_feedback'] = None
    session['study_queue'] = []      # immediate repeat feature uses this queue
    session['review_only'] = False

    return redirect(url_for('quiz'))

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    mode = session.get('mode', 'study')
    cat_id = session.get('cat_id', None)
    seed = session.get('seed')
    cursor = int(session.get('cursor', 0))
    cap = int(session.get('total_in_session', 0))
    study_queue = [int(x) for x in session.get('study_queue', [])]
    review_only = bool(session.get('review_only', False))

    if review_only and not study_queue:
        return redirect(url_for('summary'))

    pool = _build_pool(cat_id, seed, cap)
    if cursor >= len(pool) and not study_queue:
        return redirect(url_for('summary'))

    serving_from_queue = bool(study_queue)
    current_id = int(study_queue[0]) if serving_from_queue else int(pool[cursor])

    q = Question.query.get_or_404(current_id)
    qd = _question_to_dict(q)

    if request.method == 'POST':
        selected = request.form.get('answer')  # 'A'|'B'|'C'|'D'
        correct_letter = qd["correct_letter"]
        is_correct = (selected == correct_letter)

        if is_correct:
            session['correct'] = session.get('correct', 0) + 1
            session['last_feedback'] = ("correct", None)
            if serving_from_queue:
                study_queue.pop(0)
            else:
                cursor += 1
        else:
            session['wrong'] = session.get('wrong', 0) + 1
            session['last_feedback'] = ("wrong", f"{qd['correct_letter']}. {qd['correct_text']}")
            wrong_ids = set(session.get('wrong_ids', []))
            wrong_ids.add(current_id)
            session['wrong_ids'] = list(wrong_ids)
            lc = session.get('last_choice', {})
            lc[str(current_id)] = selected
            session['last_choice'] = lc

            # immediate repeat feature:
            if serving_from_queue:
                failed = study_queue.pop(0)
                study_queue.insert(0, failed)  # keep at front (repeat immediately again)
            else:
                if current_id not in study_queue:
                    study_queue.insert(0, current_id)

        session['cursor'] = cursor
        session['study_queue'] = study_queue
        return redirect(url_for('quiz'))

    # GET render
    options = qd["pairs"][:]
    random.shuffle(options)

    feedback = session.get('last_feedback')
    session['last_feedback'] = None

    total = cap
    number = min(cursor + 1, total)

    repeat = current_id in set(session.get('wrong_ids', []))

    return render_template(
        "quiz.html",
        q={"question": qd["question"], "id": qd["id"]},
        options=options,
        number=number,
        total=total,
        correct_count=session.get('correct', 0),
        wrong_count=session.get('wrong', 0),
        feedback=feedback,
        repeat=repeat
    )

@app.route('/summary')
def summary():
    mode = session.get('mode', 'study')
    total_in_session = session.get('total_in_session', 0)
    correct = session.get('correct', 0)
    wrong = session.get('wrong', 0)
    wrong_ids = session.get('wrong_ids', [])
    last_choice = session.get('last_choice', {})

    wrong_questions = {}
    if wrong_ids:
        rows = Question.query.filter(Question.id.in_(wrong_ids)).all()
        by_id = {r.id: r for r in rows}
        for qid in wrong_ids:
            r = by_id.get(qid)
            if not r:
                continue
            opts = [r.choice_a, r.choice_b, r.choice_c, r.choice_d]
            correct_letter = r.correct_answer
            your_letter = last_choice.get(str(qid))
            wrong_questions[str(qid)] = {
                'question': r.text,
                'options': opts,
                'correct_answer': {"A": opts[0], "B": opts[1], "C": opts[2], "D": opts[3]}[correct_letter],
                'your_answer': {"A": opts[0], "B": opts[1], "C": opts[2], "D": opts[3]}.get(your_letter)
            }

    return render_template(
        "summary.html",
        score=correct,
        total=total_in_session,
        correct=correct,
        wrong=wrong,
        mode=mode,
        wrong_questions=wrong_questions
    )

@app.route('/redo_wrongs')
def redo_wrongs():
    wrong_ids = [int(x) for x in session.get('wrong_ids', [])]
    if not wrong_ids:
        return redirect(url_for('start', mode='study'))
    session['mode'] = 'study'
    session['review_only'] = True
    session['study_queue'] = wrong_ids[:]
    session['total_in_session'] = len(wrong_ids)
    session['cursor'] = 0
    session['correct'] = 0
    session['wrong'] = 0
    session['wrong_ids'] = []
    session['last_choice'] = {}
    session['last_feedback'] = None
    return redirect(url_for('quiz'))

# -------- Admin: dashboard, users, questions ----------
from jinja2 import TemplateNotFound

@app.route('/admin')
@admin_required
def admin_home():
    stats = {
        "users": User.query.count(),
        "questions": Question.query.count(),
        "categories": Category.query.count(),
    }
    try:
        html = render_template("admin/dashboard.html", stats=stats)
        # If the template renders to empty/whitespace (e.g., block name mismatch), show fallback
        if not (html or "").strip():
            return (
                "<h3>Admin dashboard (empty template)</h3>"
                f"<pre>{stats}</pre>"
                '<p>Your <code>templates/admin/dashboard.html</code> rendered empty. '
                'If you use <code>{% extends \"base.html\" %}</code>, make sure the block name matches your base '
                '(e.g., <code>{% block content %}</code> / <code>{% endblock %}</code>).</p>',
                200, {"Content-Type": "text/html"}
            )
        return html
    except TemplateNotFound:
        # Missing template fallback
        return (
            "<h3>Admin dashboard (no template found)</h3>"
            f"<pre>{stats}</pre>"
            '<p>Create <code>templates/admin/dashboard.html</code> to style this.</p>',
            200, {"Content-Type": "text/html"}
        )

# Users
@app.route('/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.id.desc()).all()
    return render_template("admin/users.html", users=users)

@app.route('/admin/users/new', methods=['GET', 'POST'])
@admin_required
def admin_users_new():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        name = (request.form.get('name') or '').strip()
        level = request.form.get('level') or None
        is_admin = bool(request.form.get('is_admin'))
        pw = request.form.get('password') or ''
        if not email or not pw:
            flash("Email and password are required", "danger")
            return render_template("admin/user_form.html", user=None, categories=Category.query.all())
        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return render_template("admin/user_form.html", user=None, categories=Category.query.all())
        u = User(email=email, name=name, is_admin=is_admin, level=level)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
        flash("User created", "success")
        return redirect(url_for('admin_users'))
    return render_template("admin/user_form.html", user=None, categories=Category.query.all())

@app.route('/admin/users/<int:uid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_users_edit(uid):
    u = User.query.get_or_404(uid)
    if request.method == 'POST':
        # email change (keep unique)
        new_email = (request.form.get('email') or '').strip().lower()
        if new_email and new_email != u.email:
            if User.query.filter(User.email == new_email, User.id != u.id).first():
                flash("Email already in use.", "danger")
                return render_template("admin/user_form.html", user=u, categories=Category.query.all())
            u.email = new_email

        u.name = (request.form.get('name') or '').strip()
        u.level = request.form.get('level') or None

        # admin toggle with safety: never remove the last admin
        want_admin = bool(request.form.get('is_admin'))
        if not want_admin and u.is_admin:
            admin_count = User.query.filter_by(is_admin=True).count()
            if admin_count <= 1:
                flash("Cannot remove admin rights from the last admin.", "warning")
                return render_template("admin/user_form.html", user=u, categories=Category.query.all())
        u.is_admin = want_admin

        # password: only change if provided
        pw = request.form.get('password') or ''
        if pw:
            u.set_password(pw)

        db.session.commit()
        flash("User updated", "success")
        return redirect(url_for('admin_users'))

    return render_template("admin/user_form.html", user=u, categories=Category.query.all())


@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@admin_required
def admin_users_delete(uid):
    u = User.query.get_or_404(uid)

    # block deleting the last admin
    if u.is_admin:
        admin_count = User.query.filter_by(is_admin=True).count()
        if admin_count <= 1:
            flash("Cannot delete the last admin.", "warning")
            return redirect(url_for('admin_users'))

    # block deleting yourself (optional but sensible)
    if current_user.id == u.id:
        flash("You cannot delete yourself.", "warning")
        return redirect(url_for('admin_users'))

    db.session.delete(u)
    db.session.commit()
    flash("User deleted", "warning")
    return redirect(url_for('admin_users'))

# Questions
@app.route('/admin/questions')
@admin_required
def admin_questions():
    q = (request.args.get('q') or '').strip()
    cat = request.args.get('cat')
    qry = Question.query
    if cat:
        cat_row = Category.query.filter_by(name=cat).first()
        if cat_row:
            qry = qry.filter_by(category_id=cat_row.id)
    if q:
        like = f"%{q}%"
        qry = qry.filter(Question.text.ilike(like))
    rows = qry.order_by(Question.id.desc()).limit(500).all()
    categories = Category.query.all()
    return render_template("admin/questions.html", rows=rows, categories=categories, q=q, cat=cat)

@app.route('/admin/questions/new', methods=['GET', 'POST'])
@admin_required
def admin_questions_new():
    categories = Category.query.all()
    if request.method == 'POST':
        text = request.form.get('text') or ''
        a = request.form.get('choice_a') or ''
        b = request.form.get('choice_b') or ''
        c = request.form.get('choice_c') or ''
        d = request.form.get('choice_d') or ''
        corr = request.form.get('correct_answer') or 'A'
        cat_id = int(request.form.get('category_id')) if request.form.get('category_id') else None
        row = Question(text=text, choice_a=a, choice_b=b, choice_c=c, choice_d=d,
                       correct_answer=corr, category_id=cat_id)
        db.session.add(row)
        db.session.commit()
        flash("Question created", "success")
        return redirect(url_for('admin_questions'))
    return render_template("admin/question_form.html", row=None, categories=categories)

@app.route('/admin/questions/<int:qid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_questions_edit(qid):
    row = Question.query.get_or_404(qid)
    categories = Category.query.all()
    if request.method == 'POST':
        row.text = request.form.get('text') or row.text
        row.choice_a = request.form.get('choice_a') or row.choice_a
        row.choice_b = request.form.get('choice_b') or row.choice_b
        row.choice_c = request.form.get('choice_c') or row.choice_c
        row.choice_d = request.form.get('choice_d') or row.choice_d
        row.correct_answer = request.form.get('correct_answer') or row.correct_answer
        row.category_id = int(request.form.get('category_id')) if request.form.get('category_id') else None
        db.session.commit()
        flash("Question updated", "success")
        return redirect(url_for('admin_questions'))
    return render_template("admin/question_form.html", row=row, categories=categories)

@app.route('/admin/questions/<int:qid>/delete', methods=['POST'])
@admin_required
def admin_questions_delete(qid):
    row = Question.query.get_or_404(qid)
    db.session.delete(row)
    db.session.commit()
    flash("Question deleted", "warning")
    return redirect(url_for('admin_questions'))

# --- Debug helper ---
@app.route('/whoami')
def whoami():
    return {
        "auth": bool(current_user.is_authenticated),
        "id": getattr(current_user, "id", None),
        "email": getattr(current_user, "email", None),
        "admin": getattr(current_user, "is_admin", None),
        "session_keys": list(session.keys()),
    }

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
