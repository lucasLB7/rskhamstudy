# import_questions.py
import sys
from main import app, db, Question, Category
from questions import questions as question_list

def normalize(s: str) -> str:
    # collapse whitespace + lowercase for robust matching
    return " ".join(s.split()).strip().lower()

def find_correct_index(options, correct_text):
    ct = normalize(correct_text)
    for i, opt in enumerate(options):
        if normalize(opt) == ct:
            return i
    return -1  # not found

def import_questions(default_category="Unassigned"):
    with app.app_context():
        print(f"[INFO] Starting import. Total source questions: {len(question_list)}")

        # Create/find category
        cat = Category.query.filter_by(name=default_category).first()
        if not cat:
            cat = Category(name=default_category)
            db.session.add(cat)
            db.session.flush()
            print(f"[INFO] Created category '{default_category}' (id={cat.id})")
        else:
            print(f"[INFO] Using category '{default_category}' (id={cat.id})")

        # If DB already has questions, warn (prevents duplicate imports)
        existing = Question.query.count()
        if existing:
            print(f"[WARN] Database already has {existing} questions. "
                  f"Continuing will ADD more (possible duplicates).")
            # You can return here if you want to block duplicates:
            # return

        created = 0
        skipped = 0

        for idx, q in enumerate(question_list, start=1):
            try:
                opts = q["options"]
                correct_text = q["answer"]

                if len(opts) != 4:
                    print(f"[SKIP] Q#{idx}: options length != 4")
                    skipped += 1
                    continue

                i = find_correct_index(opts, correct_text)
                if i == -1:
                    # Still import, default to first option as correct to avoid crash
                    print(f"[WARN] Q#{idx}: correct answer not found in options. "
                          f"Defaulting to 'A'. question='{q['question'][:60]}...'")
                    letter = "A"
                else:
                    letter = "ABCD"[i]

                new_q = Question(
                    text=q["question"],
                    choice_a=opts[0],
                    choice_b=opts[1],
                    choice_c=opts[2],
                    choice_d=opts[3],
                    correct_answer=letter,
                    category_id=cat.id,
                )
                db.session.add(new_q)
                created += 1

                if created % 100 == 0:
                    db.session.flush()
                    print(f"[INFO] Imported {created} so far...")

            except Exception as e:
                print(f"[ERROR] Q#{idx}: {e}")
                skipped += 1

        db.session.commit()
        print(f"[DONE] Imported {created} questions into category '{default_category}'. "
              f"Skipped {skipped}. Total now in DB: {Question.query.count()}")

if __name__ == "__main__":
    try:
        default_cat = sys.argv[1] if len(sys.argv) > 1 else "Unassigned"
    except Exception:
        default_cat = "Unassigned"
    import_questions(default_cat)
