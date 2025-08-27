from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    # Optional learning level; when set, /start defaults to this category
    level = db.Column(db.String(32), nullable=True)  # 'Foundation' | 'Intermediary' | 'Full licence'

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # allow image-only stems
    text = db.Column(db.Text, nullable=False)

    choice_a = db.Column(db.Text, nullable=False)
    choice_b = db.Column(db.Text, nullable=False)
    choice_c = db.Column(db.Text, nullable=False)
    choice_d = db.Column(db.Text, nullable=False)

    correct_answer = db.Column(db.String(1), nullable=False)  # 'A'/'B'/'C'/'D'

    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    category = db.relationship('Category', lazy='joined')

    # NEW image URL fields
    question_image_url = db.Column(db.Text)
    choice_a_image_url = db.Column(db.Text)
    choice_b_image_url = db.Column(db.Text)
    choice_c_image_url = db.Column(db.Text)
    choice_d_image_url = db.Column(db.Text)