from flask import Flask, render_template, request, redirect, url_for, session
from questions import questions
import random
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"

# ---- Important: prevent Flask from sorting JSON keys (avoids int<->str comparisons) ----
app.config['JSON_SORT_KEYS'] = False
try:
    # Flask 2.3+ JSON provider
    app.json.sort_keys = False
except Exception:
    pass
# ----------------------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/start/<mode>')
def start(mode):
    total_questions = len(questions)

    indices = list(range(total_questions))
    random.shuffle(indices)

    # exam mode capped at 60 questions
    if mode == "exam" and total_questions > 60:
        indices = indices[:60]

    # reset tracking every new session
    session.clear()
    session['total_in_session'] = len(indices)
    session['mode'] = mode
    session['remaining'] = indices  # pool of active questions

    # scoring / tracking
    session['score'] = 0
    session['correct'] = 0
    session['wrong'] = 0

    # sets (stored as lists) to track questions
    session['wrong_once'] = []      # questions that have been wrong at least once
    session['correct_set'] = []     # questions that have been answered correctly at least once

    # review data + feedback
    session['wrong_questions'] = {} # {qid(str): {question, options, correct_answer, your_answer}}
    session['last_feedback'] = None

    # per-attempt answer log (source of truth for summary)
    session['answered'] = []        # list of {"id", "chosen", "correct_answer", "is_correct"}

    return redirect(url_for('quiz'))

@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    if 'remaining' not in session or not session['remaining']:
        return redirect(url_for('summary'))

    remaining = session['remaining']
    current = remaining[0]             # int index into questions
    mode = session.get('mode', 'study')

    if request.method == 'POST':
        selected = request.form.get('answer')
        correct_answer = questions[current]['answer']

        # pull state
        wrong_once = set(session.get('wrong_once', []))
        correct_set = set(session.get('correct_set', []))
        wrong_questions = session.get('wrong_questions', {})
        answered = session.get('answered', [])

        # ---- Normalize wrong_questions keys to strings (belt & suspenders) ----
        wrong_questions = {str(k): v for k, v in wrong_questions.items()}
        qid_str = str(current)
        # ----------------------------------------------------------------------

        # grade
        is_correct = (selected == correct_answer)
        answered.append({
            "id": current,
            "chosen": selected,
            "correct_answer": correct_answer,
            "is_correct": is_correct
        })
        session['answered'] = answered

        if is_correct:
            session['last_feedback'] = ("correct", None)
            if current not in correct_set:
                correct_set.add(current)
                session['correct_set'] = list(correct_set)
            # always remove once answered correctly
            remaining.pop(0)
        else:
            session['last_feedback'] = ("wrong", correct_answer)

            # Save/Update for review (store the latest wrong choice)
            if qid_str not in wrong_questions:
                wrong_questions[qid_str] = {
                    'question': questions[current]['question'],
                    'options': questions[current]['options'],
                    'correct_answer': correct_answer,
                    'your_answer': selected
                }
            else:
                # keep this updated with the last wrong selection
                wrong_questions[qid_str]['your_answer'] = selected
            session['wrong_questions'] = wrong_questions

            # mark as wrong-at-least-once (so banner appears NEXT time immediately)
            if current not in wrong_once:
                wrong_once.add(current)
                session['wrong_once'] = list(wrong_once)

            # advance deck depending on mode
            if mode == "exam":
                remaining.pop(0)  # move on in exam
            else:
                # in study, reinsert for retry later
                remaining.insert(random.randint(1, len(remaining)), remaining.pop(0))

        # persist remaining and redirect
        session['remaining'] = remaining
        return redirect(url_for('quiz'))

    # GET: show question
    q = questions[current]
    options = q['options'].copy()
    random.shuffle(options)

    feedback = session.get('last_feedback')
    session['last_feedback'] = None

    # progress indicator: based on unique seen (correct_set ∪ wrong_once)
    answered_unique = len(set(session.get('correct_set', [])) | set(session.get('wrong_once', [])))
    total = session.get('total_in_session', len(questions))
    number = min(answered_unique + 1, total)

    # banner should show if this question has *ever* been wrong before
    repeat = current in set(session.get('wrong_once', []))

    return render_template(
        "quiz.html",
        q=q,
        options=options,
        number=number,
        total=total,
        correct_count=len(set(session.get('correct_set', []))),
        wrong_count=len(set(session.get('wrong_once', []))),
        feedback=feedback,
        repeat=repeat
    )

@app.route('/summary')
def summary():
    mode = session.get('mode', 'study')
    total_in_session = session.get('total_in_session', 0)
    wrong_questions = session.get('wrong_questions', {})

    # Use the per-attempt log for accurate scoring
    answered = session.get('answered', [])
    correct = sum(1 for a in answered if a.get("is_correct"))
    wrong = len(answered) - correct

    score = correct  # show correct out of total_in_session for both modes

    return render_template(
        "summary.html",
        score=score,
        total=total_in_session,
        correct=correct,
        wrong=wrong,
        mode=mode,
        wrong_questions=wrong_questions
    )

@app.route('/redo_wrongs')
def redo_wrongs():
    wrong_questions = session.get('wrong_questions', {})
    if not wrong_questions:
        return redirect(url_for('start', mode='study'))

    # Build IDs safely from string keys
    wrong_ids = []
    for k in wrong_questions.keys():
        s = str(k)
        if s.isdigit():
            wrong_ids.append(int(s))

    if not wrong_ids:
        return redirect(url_for('start', mode='study'))

    session['mode'] = 'study'
    session['remaining'] = wrong_ids[:]  # just the wrong ones
    session['total_in_session'] = len(wrong_ids)
    session['correct_set'] = []
    session['wrong_once'] = []
    session['answered'] = []
    session['last_feedback'] = None
    return redirect(url_for('quiz'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
