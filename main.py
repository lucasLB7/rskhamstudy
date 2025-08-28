# Dependencies and critical libraries (see requirements.txt)
from flask import Flask, render_template, request, redirect, url_for, session, abort, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from pathlib import Path
import random, os
from models import db, User, Category, Question
from sqlalchemy import func
from functools import wraps
from datetime import datetime, timezone
from flask_migrate import Migrate
from werkzeug.datastructures import FileStorage
try:
    from storage import upload_image  # your GCS helper
except Exception:
    upload_image = None  # fallback if running locally without GCS
from werkzeug.utils import secure_filename

# --- App + config ---
app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "supersecretkey")

# Limit uploads (adjust if needed)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB

# JSON sort behavior
app.config["JSON_SORT_KEYS"] = False
try:
    app.json.sort_keys = False
except Exception:
    pass

# Auto-reload templates in dev
app.config["TEMPLATES_AUTO_RELOAD"] = True

# --- Database config (GAE-friendly, Cloud SQL-ready) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    # Otherwise, default to SQLite.
    on_gae = os.environ.get("GAE_ENV", "").startswith("standard")
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    data_dir = Path("/tmp") if (on_gae or on_cloud_run) else Path(app.instance_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "rskhamstudy.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path.as_posix()}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}

# Initialize DB + migrations
db.init_app(app)
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

# ---------- Seeding ----------
def seed_defaults():
    """Idempotent seeds for categories and the first admin."""
    if not Category.query.first():
        for name in ["Foundation", "Intermediary", "Full licence", "Unassigned"]:
            db.session.add(Category(name=name))
        db.session.commit()

    if not User.query.filter_by(is_admin=True).first():
        admin = User(email="admin@local", name="Admin", is_admin=True, level=None)
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "changeme"))
        db.session.add(admin)
        db.session.commit()
        app.logger.info("Seeded admin user: admin@local (password from ADMIN_PASSWORD).")

# Ensure tables + seed exactly once per worker
_init_ran = False
def _ensure_db_seeded_once():
    global _init_ran
    if _init_ran:
        return
    with app.app_context():
        try:
            db.create_all()
            seed_defaults()
        except Exception as e:
            app.logger.warning(f"DB init/seed skipped or failed: {e}")
    _init_ran = True
_ensure_db_seeded_once()

# ---------- Quiz helpers ----------
def _question_to_dict(q: Question):
    texts = [q.choice_a, q.choice_b, q.choice_c, q.choice_d]
    imgs  = [
        getattr(q, "choice_a_image_url", None),
        getattr(q, "choice_b_image_url", None),
        getattr(q, "choice_c_image_url", None),
        getattr(q, "choice_d_image_url", None),
    ]
    letters = ["A", "B", "C", "D"]
    pairs = [(L, t, imgs[i]) for i, (L, t) in enumerate(zip(letters, texts))]
    correct_letter = q.correct_answer
    correct_text = {"A": q.choice_a, "B": q.choice_b, "C": q.choice_c, "D": q.choice_d}[correct_letter]
    return {
        "id": q.id,
        "question": q.text,
        "question_image_url": getattr(q, "question_image_url", None),
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

# ---------- Level helpers (permissions & synonyms) ----------
LEVEL_SYNONYMS = {
    "Novice": "Foundation",
    "Foundation": "Foundation",
    "Intermediary": "Intermediary",
    "Full": "Full licence",
    "Full license": "Full licence",
    "Full licence": "Full licence",
}

def normalize_level(level: str | None) -> str | None:
    if not level:
        return None
    return LEVEL_SYNONYMS.get(level.strip(), level.strip())

def allowed_category_names(user_level: str | None) -> set[str]:
    """
    Rules:
      - Foundation/Novice -> {'Foundation'}
      - Intermediary      -> {'Foundation', 'Intermediary'}
      - Full licence      -> all categories except 'Unassigned'
      - None/unknown      -> empty set
    """
    user_level = normalize_level(user_level)
    all_names = [c.name for c in Category.query.order_by(Category.name.asc()).all()]
    visible = [n for n in all_names if n != "Unassigned"]

    if user_level == "Foundation":
        allowed = {"Foundation"}
    elif user_level == "Intermediary":
        allowed = {"Foundation", "Intermediary"}
    elif user_level == "Full licence":
        allowed = set(visible)
    else:
        allowed = set()

    return allowed & set(visible)

# ---------- Auth & guards ----------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.path))
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

# ---------- Upload helpers (centralized & safe) ----------
def _running_on_app_engine() -> bool:
    # True on GAE standard; also consider Cloud Run env var
    return bool(os.getenv("GAE_ENV") == "standard" or os.getenv("K_SERVICE"))

def _save_local(fs, folder: str) -> str | None:
    """Fallback when storage.upload_image is not available: save under /static/uploads/<folder>/."""
    if not fs or not getattr(fs, "filename", ""):
        return None
    uploads_root = Path(app.static_folder) / "uploads" / folder
    uploads_root.mkdir(parents=True, exist_ok=True)
    fname = secure_filename(fs.filename)
    # Optional: uniquify
    stem = Path(fname).stem
    suffix = Path(fname).suffix
    i = 1
    out = uploads_root / fname
    while out.exists():
        fname = f"{stem}_{i}{suffix}"
        out = uploads_root / fname
        i += 1
    fs.save(out.as_posix())
    # Return a URL the browser can load
    return url_for("static", filename=f"uploads/{folder}/{fname}")

def _upload_file(fs: FileStorage | None, folder: str) -> str | None:
    """
    Upload to GCS in prod; locally fall back to /static/uploads.
    Returns:
      - URL string on success
      - None if no file provided
      - "__UPLOAD_FAILED__" sentinel if an exception occurred (and flashes a message)
    """
    if not fs or not getattr(fs, "filename", ""):
        return None
    try:
        if _running_on_app_engine():
            app.logger.info("Upload path: GCS")
            if not upload_image:
                raise RuntimeError("upload_image helper not available in prod.")
            return upload_image(fs, folder=folder)
        else:
            app.logger.info("Upload path: LOCAL")
            return _save_local(fs, folder)
    except Exception as e:
        app.logger.exception(f"Image upload failed for folder={folder}")
        flash(f"Image upload failed: {e}", "danger")
        return "__UPLOAD_FAILED__"

# ---------- Routes ----------

# Landing: require login
@app.route('/')
@login_required
def index():
    level = normalize_level(getattr(current_user, "level", None))
    # Hide "Unassigned" from user selection
    categories = [c for c in Category.query.order_by(Category.name.asc()).all() if c.name != "Unassigned"]
    last_login = session.get("last_login_display")  # may be None on first login
    allowed = allowed_category_names(level)  # set[str]
    return render_template(
        "index.html",
        level=level,
        categories=categories,
        last_login=last_login,
        allowed_names=allowed,
    )

# Login/Logout
@app.route('/login', methods=['GET', 'POST'])
def login():
    # If already logged in, send to the right place
    if request.method == 'GET':
        if current_user.is_authenticated:
            return redirect(url_for('admin_home') if current_user.is_admin else url_for('index'))
        return render_template("login.html")  # <-- IMPORTANT: return!

    # POST: authenticate
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("Invalid credentials", "danger")
        return render_template("login.html")  # <-- return on failure

    login_user(user)
    flash("Welcome back!", "success")

    # Optional: last login tracking (kept from your code)
    fmt = "%b %d, %Y %H:%M"
    prev_display = None
    try:
        if hasattr(User, "last_login_at"):
            prev = getattr(user, "last_login_at", None)
            if prev:
                try:
                    prev_display = prev.astimezone().strftime(fmt)
                except Exception:
                    prev_display = prev.strftime(fmt)
            user.last_login_at = datetime.now(timezone.utc)
            db.session.commit()
        else:
            prev_display = session.get("last_login_current")
            session["last_login_current"] = datetime.now().strftime(fmt)
    except Exception:
        prev_display = session.get("last_login_current")
    session["last_login_display"] = prev_display

    # Redirect target
    next_url = request.args.get('next')
    if not next_url:
        next_url = url_for('admin_home') if user.is_admin else url_for('index')
    return redirect(next_url)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Signed out", "info")
    return redirect(url_for('login'))

@app.route('/me/level', methods=['POST'])
@login_required
def update_level():
    raw_level = (request.form.get('level') or '').strip()
    normalized = normalize_level(raw_level)
    if not raw_level:
        normalized = None

    if normalized and not Category.query.filter_by(name=normalized).first():
        flash("Invalid level.", "danger")
        return redirect(url_for('index'))

    current_user.level = normalized
    db.session.commit()
    flash("Level updated.", "success")
    return redirect(url_for('index'))

# Start quiz (requires login)
@app.route('/start/<mode>')
@login_required
def start(mode):
    req_cat = request.args.get("cat") or None
    user_level = normalize_level(getattr(current_user, "level", None))
    allowed = allowed_category_names(user_level)

    # Decide category to run with
    cat_name = None
    if req_cat:
        if req_cat in allowed:
            cat_name = req_cat
            if current_user.level != req_cat:
                current_user.level = req_cat
                db.session.commit()
        else:
            if user_level in allowed:
                cat_name = user_level
                flash("That category is not permitted for your level. Using your default level instead.", "warning")
            else:
                flash("No permitted categories for your account. Ask an admin to assign a level.", "danger")
    else:
        if user_level in allowed:
            cat_name = user_level
        elif allowed:
            cat_name = sorted(allowed)[0]
        else:
            flash("No permitted categories for your account. Ask an admin to assign a level.", "danger")

    # Resolve category id (if chosen)
    cat_id = None
    if cat_name:
        cat_row = Category.query.filter_by(name=cat_name).first()
        if not cat_row:
            abort(404)
        cat_id = cat_row.id
    else:
        return redirect(url_for('index'))

    base = Question.query
    if cat_id:
        base = base.filter_by(category_id=cat_id)
    total_available = base.count()
    cap = min(60, total_available) if mode == "exam" else total_available

    _clear_quiz_session()
    session.update({
        'mode': mode,
        'cat_id': cat_id,
        'seed': random.randrange(2**31),
        'cursor': 0,
        'total_in_session': cap,
        'correct': 0,
        'wrong': 0,
        'wrong_ids': [],
        'last_choice': {},
        'last_feedback': None,
        'study_queue': [],
        'review_only': False,
    })
    return redirect(url_for('quiz'))

@app.route('/quiz', methods=['GET', 'POST'])
@login_required
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

            # One-shot suppression of the "previously wrong" banner on the immediate re-render
            session['suppress_repeat_banner'] = current_id

            if mode == 'study':
                # Re-queue to the END (avoid double-in-a-row)
                if serving_from_queue:
                    failed = study_queue.pop(0)
                    study_queue.append(failed)
                else:
                    if current_id not in study_queue:
                        study_queue.append(current_id)
                # Don't advance cursor here; we'll see other items first
            else:
                # Exam/Test mode: record and move on; no immediate repeat
                if serving_from_queue:
                    study_queue.pop(0)
                cursor += 1

        session['cursor'] = cursor
        session['study_queue'] = study_queue
        return redirect(url_for('quiz'))

    # GET render
    options = qd["pairs"][:]      # keep (letter, text, image_url)
    random.shuffle(options)

    feedback = session.get('last_feedback')
    session['last_feedback'] = None

    total = cap
    number = min(cursor + 1, total)

    # Show yellow banner for previously wrong,
    # except suppress it once immediately after a wrong submission
    wrong_ids_set = set(session.get('wrong_ids', []))
    suppress_id = session.pop('suppress_repeat_banner', None)
    repeat = (current_id in wrong_ids_set) and (suppress_id is None or current_id != suppress_id)

    return render_template(
        "quiz.html",
        q={"question": qd["question"], "id": qd["id"]},
        q_stem_text=qd["question"],
        q_stem_img=qd.get("question_image_url"),
        options=options,
        number=number,
        total=total,
        correct_count=session.get('correct', 0),
        wrong_count=session.get('wrong', 0),
        feedback=feedback,
        repeat=repeat
    )

@app.route('/summary')
@login_required
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
                'question_image_url': getattr(r, 'question_image_url', None),
                'options': opts,
                'option_images': [
                    getattr(r, 'choice_a_image_url', None),
                    getattr(r, 'choice_b_image_url', None),
                    getattr(r, 'choice_c_image_url', None),
                    getattr(r, 'choice_d_image_url', None),
                ],
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
@login_required
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
@app.route('/admin')
@admin_required
def admin_home():
    stats = {
        "users": User.query.count(),
        "questions": Question.query.count(),
        "categories": Category.query.count(),
        "unassigned": Question.query.filter_by(category_id=None).count(),
    }
    by_cat = (
        db.session.query(Category.name, func.count(Question.id))
        .outerjoin(Question, Question.category_id == Category.id)
        .group_by(Category.id)
        .order_by(Category.name.asc())
        .all()
    )
    latest_users = User.query.order_by(User.id.desc()).limit(5).all()
    latest_questions = Question.query.order_by(Question.id.desc()).limit(5).all()
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        by_cat=by_cat,
        latest_users=latest_users,
        latest_questions=latest_questions,
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
        new_email = (request.form.get('email') or '').strip().lower()
        if new_email and new_email != u.email:
            if User.query.filter(User.email == new_email, User.id != u.id).first():
                flash("Email already in use.", "danger")
                return render_template("admin/user_form.html", user=u, categories=Category.query.all())
            u.email = new_email

        u.name = (request.form.get('name') or '').strip()
        u.level = request.form.get('level') or None

        want_admin = bool(request.form.get('is_admin'))
        if not want_admin and u.is_admin:
            admin_count = User.query.filter_by(is_admin=True).count()
            if admin_count <= 1:
                flash("Cannot remove admin rights from the last admin.", "warning")
                return render_template("admin/user_form.html", user=u, categories=Category.query.all())
        u.is_admin = want_admin

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
    if u.is_admin:
        admin_count = User.query.filter_by(is_admin=True).count()
        if admin_count <= 1:
            flash("Cannot delete the last admin.", "warning")
            return redirect(url_for('admin_users'))
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

@app.route('/admin/questions/bulk', methods=['POST'])
@admin_required
def admin_questions_bulk():
    ids = [int(x) for x in request.form.getlist('ids')]
    val = request.form.get('category_id', '').strip()
    if not ids:
        flash("No questions selected.", "warning")
        return redirect(url_for('admin_questions'))

    # Resolve new category
    if val == "__clear__":
        new_cat_id = None
        cat_name = "(none)"
    else:
        if not val.isdigit():
            flash("Pick a category to assign.", "warning")
            return redirect(url_for('admin_questions'))
        new_cat_id = int(val)
        cat = Category.query.get(new_cat_id)
        if not cat:
            flash("Category not found.", "danger")
            return redirect(url_for('admin_questions'))
        cat_name = cat.name

    rows = Question.query.filter(Question.id.in_(ids)).all()
    for r in rows:
        r.category_id = new_cat_id
    db.session.commit()
    flash(f"Updated {len(rows)} question(s) → {cat_name}", "success")
    return redirect(url_for('admin_questions'))

@app.route('/admin/questions/new', methods=['GET', 'POST'])
@admin_required
def admin_questions_new():
    categories = Category.query.all()
    if request.method == 'POST':
        text = (request.form.get('text') or '').strip()
        a = (request.form.get('choice_a') or '').strip()
        b = (request.form.get('choice_b') or '').strip()
        c = (request.form.get('choice_c') or '').strip()
        d = (request.form.get('choice_d') or '').strip()
        corr = (request.form.get('correct_answer') or 'A').strip().upper()
        cat_id = int(request.form['category_id']) if request.form.get('category_id') else None

        if corr not in {"A","B","C","D"}:
            flash("Correct answer must be A, B, C or D.", "danger")
            return render_template("admin/question_form.html", row=None, categories=categories)

        # Files
        qi = request.files.get('question_image')  # type: FileStorage
        ai = request.files.get('choice_a_image')
        bi = request.files.get('choice_b_image')
        ci = request.files.get('choice_c_image')
        di = request.files.get('choice_d_image')

        # Uploads (centralized helper)
        q_url = _upload_file(qi, "question_stems")
        a_url = _upload_file(ai, "choice_images")
        b_url = _upload_file(bi, "choice_images")
        c_url = _upload_file(ci, "choice_images")
        d_url = _upload_file(di, "choice_images")

        # If any upload failed, re-render (friendly flash already shown)
        if "__UPLOAD_FAILED__" in {q_url, a_url, b_url, c_url, d_url}:
            return render_template("admin/question_form.html", row=None, categories=categories)

        # Require at least stem text or stem image
        if not (text or q_url):
            flash("Provide question text or an image.", "danger")
            return render_template("admin/question_form.html", row=None, categories=categories)

        # Each choice must have text or an image
        def ok(txt, url): return (txt and txt.strip()) or url
        if not all([ok(a, a_url), ok(b, b_url), ok(c, c_url), ok(d, d_url)]):
            flash("Each choice must have text or an image.", "danger")
            return render_template("admin/question_form.html", row=None, categories=categories)

        row = Question(
            text=text,
            choice_a=a, choice_b=b, choice_c=c, choice_d=d,
            correct_answer=corr, category_id=cat_id,
            question_image_url=q_url,
            choice_a_image_url=a_url,
            choice_b_image_url=b_url,
            choice_c_image_url=c_url,
            choice_d_image_url=d_url
        )
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
        # Text fields
        row.text = (request.form.get('text') or '').strip()
        row.choice_a = (request.form.get('choice_a') or '').strip()
        row.choice_b = (request.form.get('choice_b') or '').strip()
        row.choice_c = (request.form.get('choice_c') or '').strip()
        row.choice_d = (request.form.get('choice_d') or '').strip()
        row.correct_answer = (request.form.get('correct_answer') or row.correct_answer).strip().upper()
        row.category_id = int(request.form['category_id']) if request.form.get('category_id') else None

        # Removal toggles
        rm_q = bool(request.form.get('remove_question_image'))
        rm_a = bool(request.form.get('remove_choice_a_image'))
        rm_b = bool(request.form.get('remove_choice_b_image'))
        rm_c = bool(request.form.get('remove_choice_c_image'))
        rm_d = bool(request.form.get('remove_choice_d_image'))

        if rm_q: row.question_image_url = None
        if rm_a: row.choice_a_image_url = None
        if rm_b: row.choice_b_image_url = None
        if rm_c: row.choice_c_image_url = None
        if rm_d: row.choice_d_image_url = None

        # New uploads (replace existing)
        qi = request.files.get('question_image')
        ai = request.files.get('choice_a_image')
        bi = request.files.get('choice_b_image')
        ci = request.files.get('choice_c_image')
        di = request.files.get('choice_d_image')

        q_url = _upload_file(qi, "question_stems")
        a_url = _upload_file(ai, "choice_images")
        b_url = _upload_file(bi, "choice_images")
        c_url = _upload_file(ci, "choice_images")
        d_url = _upload_file(di, "choice_images")

        # If any upload failed, re-render (friendly flash already shown)
        if "__UPLOAD_FAILED__" in {q_url, a_url, b_url, c_url, d_url}:
            return render_template("admin/question_form.html", row=row, categories=categories)

        if q_url: row.question_image_url = q_url
        if a_url: row.choice_a_image_url = a_url
        if b_url: row.choice_b_image_url = b_url
        if c_url: row.choice_c_image_url = c_url
        if d_url: row.choice_d_image_url = d_url

        # Final validation: require stem text or image
        if not (row.text or row.question_image_url):
            flash("Provide question text or an image.", "danger")
            return render_template("admin/question_form.html", row=row, categories=categories)

        # Each choice must have text or image
        def ok(txt, url): return (txt and txt.strip()) or url
        if not all([
            ok(row.choice_a, row.choice_a_image_url),
            ok(row.choice_b, row.choice_b_image_url),
            ok(row.choice_c, row.choice_c_image_url),
            ok(row.choice_d, row.choice_d_image_url),
        ]):
            flash("Each choice must have text or an image.", "danger")
            return render_template("admin/question_form.html", row=row, categories=categories)

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

# --- Debug helpers ---
@app.route('/__tpl_src')
def __tpl_src():
    name = request.args.get('name', 'admin/questions.html')
    try:
        src, filename, _ = app.jinja_loader.get_source(app.jinja_env, name)
        return {"name": name, "filename": filename, "size": len(src), "head": src[:200]}
    except Exception as e:
        return {"name": name, "error": repr(e)}, 500

@app.route('/whoami')
def whoami():
    return {
        "auth": bool(current_user.is_authenticated),
        "id": getattr(current_user, "id", None),
        "email": getattr(current_user, "email", None),
        "admin": getattr(current_user, "is_admin", None),
        "session_keys": list(session.keys()),
    }

# ---- One-off CLI: import questions at scale ----
import click, json, importlib.util, pathlib

def _ensure_category(name: str | None):
    if not name:
        return None
    cat = Category.query.filter_by(name=name).first()
    if not cat:
        cat = Category(name=name)
        db.session.add(cat)
        db.session.flush()
    return cat.id

def _to_letter_from_index(idx: int):
    return {0: "A", 1: "B", 2: "C", 3: "D"}.get(idx)

def _normalize_item(item):
    """
    Accept common shapes and return:
      text, [a,b,c,d], correct_letter, category_name
    """
    # text / question
    text = (item.get("text") or item.get("question") or "").strip()
    if not text:
        return None

    # options / choices
    if all(k in item for k in ("choice_a", "choice_b", "choice_c", "choice_d")):
        choices = [item["choice_a"], item["choice_b"], item["choice_c"], item["choice_d"]]
    elif all(k in item for k in ("a", "b", "c", "d")):
        choices = [item["a"], item["b"], item["c"], item["d"]]
    elif isinstance(item.get("options"), list) and len(item["options"]) == 4:
        choices = item["options"]
    elif isinstance(item.get("choices"), list) and len(item["choices"]) == 4:
        choices = item["choices"]
    else:
        return None

    # correct answer: letter OR index OR exact option text
    corr = item.get("correct_answer") or item.get("correct") or item.get("answer")
    corr_letter = None
    if isinstance(corr, int) and corr in (0,1,2,3):
        corr_letter = _to_letter_from_index(corr)
    elif isinstance(corr, str):
        s = corr.strip()
        # letter?
        if s.upper() in {"A","B","C","D"}:
            corr_letter = s.upper()
        # numeric-string?
        elif s.isdigit() and int(s) in (0,1,2,3):
            corr_letter = _to_letter_from_index(int(s))
        else:
            # match by text
            try:
                idx = choices.index(s)
                corr_letter = _to_letter_from_index(idx)
            except ValueError:
                corr_letter = None

    if corr_letter not in {"A","B","C","D"}:
        return None

    cat_name = (item.get("category") or "").strip() or None
    return text, choices, corr_letter, cat_name

def _iter_items_from_path(path: str):
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() == ".py":
        # import module and read a top-level `questions`
        spec = importlib.util.spec_from_file_location("qs_mod", str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        data = getattr(mod, "questions", None)
        if not isinstance(data, (list, tuple)):
            raise ValueError("questions.py must define a top-level list named `questions`")
        return list(data)
    else:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "questions" in data and isinstance(data["questions"], list):
            return data["questions"]
        if isinstance(data, list):
            return data
        raise ValueError("JSON must be a list, or an object with `questions: [...]`")

@app.cli.command("import-questions")
@click.argument("path", type=str)
@click.option("--default-category", default=None, help="Category to use if item has none.")
@click.option("--skip-duplicates", is_flag=True, help="Skip if same text already exists.")
def import_questions_cli(path, default_category, skip_duplicates):
    """
    Import a large questions file (JSON or Python module with `questions = [...]`).
    Usage:
      flask --app main import-questions ./questions.py
    """
    with app.app_context():
        items = _iter_items_from_path(path)
        seen = set()
        imported = 0
        skipped = 0

        # Optional prefetch existing texts for duplicate check
        existing_texts = set()
        if skip_duplicates:
            existing_texts = {t for (t,) in db.session.query(Question.text).all()}

        for raw in items:
            norm = _normalize_item(raw)
            if not norm:
                skipped += 1
                continue
            text, choices, corr_letter, cat_name = norm

            # duplicate prevention (by text)
            if skip_duplicates and (text in existing_texts or text in seen):
                skipped += 1
                continue

            # map choices
            a,b,c,d = choices
            # category resolve/create
            cat_id = _ensure_category(cat_name or default_category)

            q = Question(text=text,
                         choice_a=a, choice_b=b, choice_c=c, choice_d=d,
                         correct_answer=corr_letter,
                         category_id=cat_id)
            db.session.add(q)

            seen.add(text)
            imported += 1

            # periodic flush to keep memory/transaction in check
            if imported % 500 == 0:
                db.session.flush()

        db.session.commit()
        click.echo(f"Imported: {imported}, Skipped: {skipped}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
