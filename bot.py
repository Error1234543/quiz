#!/usr/bin/env python3
# bot.py - Telegram MCQ→Quiz bot (Render-ready)
# Features:
#  - Accept PDF, extract MCQs (PyMuPDF + pytesseract fallback)
#  - Ask user to set quiz duration (minutes). After setting, bot sends all polls immediately.
#  - Quiz runs for the chosen duration. When time is up, bot computes per-user results and posts them.
#  - /solve {n} uses LLM (placeholder) to return explanation.
#
# Configure via environment variables:
# BOT_TOKEN (required), GEMINI_API_KEY (optional), OWNER_ID (optional), FORWARD_BACKUP_CHANNEL_ID (optional)
#
# NOTE: Replace get_answer_with_gemini() implementation with your provider endpoint.

import os
import re
import io
import time
import json
import threading
import sqlite3
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

import telebot
from telebot import types
import fitz
from PIL import Image
import pytesseract
import requests

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID")) if os.environ.get("OWNER_ID") else None
FORWARD_BACKUP_CHANNEL_ID = int(os.environ.get("FORWARD_BACKUP_CHANNEL_ID")) if os.environ.get("FORWARD_BACKUP_CHANNEL_ID") else None

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# --- Database ---
DB_PATH = "quizbot.db"
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        chat_id INTEGER,
        session_id TEXT,
        questions_json TEXT,
        current_q INTEGER,
        scores_json TEXT,
        start_ts INTEGER,
        end_ts INTEGER,
        PRIMARY KEY (chat_id, session_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS polls_map (
        chat_id INTEGER,
        poll_id TEXT,
        session_id TEXT,
        q_index INTEGER
    )""")
    conn.commit()
    return conn
DB = init_db()

# --- Utilities: PDF text extraction + OCR fallback ---
def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_texts = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        text = page.get_text().strip()
        if text and len(text) > 20:
            out_texts.append(text)
        else:
            # render and OCR
            zoom = 2
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            try:
                ocr_text = pytesseract.image_to_string(img, lang='eng+guj')
            except Exception:
                ocr_text = pytesseract.image_to_string(img)
            out_texts.append(ocr_text)
    return "\n\n".join(out_texts)

# --- MCQ parsing (heuristic) ---
OPTION_REGEX = re.compile(r'(?:\n|\r|^)\s*([A-DA-Da-d1-4])[\).\-\s:]{1,3}\s*(.+?)(?=(?:\n\s*[A-DA-Da-d1-4][\).\-\s:])|$)', re.S)

def parse_mcqs_from_text(text: str) -> List[Dict]:
    items = []
    parts = re.split(r'\n(?=\s*\d{1,3}[\.\)\-]\s)', text)
    for part in parts:
        m = re.match(r'\s*(\d{1,3})[\.\)\-]\s*(.+)', part, re.S)
        if not m:
            continue
        num = m.group(1)
        rest = m.group(2).strip()
        opts = OPTION_REGEX.findall("\n" + rest)
        options = [o[1].strip().replace("\n", " ") for o in opts]
        question_text = rest
        if opts:
            first_opt_pat = re.search(r'(?:\n|\r)\s*[A-Da-d1-4][\).\-\s:]{1,3}', rest)
            if first_opt_pat:
                question_text = rest[:first_opt_pat.start()].strip()
        if options:
            items.append({'num': int(num), 'question': question_text, 'options': options, 'correct_index': None, 'explanation': None})
    return items

# --- LLM placeholder ---
def get_answer_with_gemini(question: str, options: List[str]) -> Tuple[Optional[int], str]:
    # Placeholder: returns index 0 and an explanatory note.
    # Replace this with a real API request to Gemini/OpenAI.
    if not GEMINI_API_KEY:
        return 0, "(GEMINI_API_KEY not set) defaulting to option A. Please configure LLM API for real answers."
    try:
        # Add your provider-specific request here.
        return 0, "(demo) choose A — replace with real LLM call."
    except Exception as e:
        return None, f"LLM call failed: {e}"

# --- DB helpers ---
def save_session(chat_id: int, session_id: str, questions: List[Dict], current_q: int = 0, scores: dict = None, start_ts:int=None, end_ts:int=None):
    conn = DB
    cur = conn.cursor()
    qjson = json.dumps(questions, ensure_ascii=False)
    scores_json = json.dumps(scores or {}, ensure_ascii=False)
    cur.execute("REPLACE INTO sessions (chat_id, session_id, questions_json, current_q, scores_json, start_ts, end_ts) VALUES (?,?,?,?,?,?,?)",
                (chat_id, session_id, qjson, current_q, scores_json, start_ts or 0, end_ts or 0))
    conn.commit()

def load_session(chat_id: int, session_id: str):
    conn = DB
    cur = conn.cursor()
    cur.execute("SELECT questions_json, current_q, scores_json, start_ts, end_ts FROM sessions WHERE chat_id=? AND session_id=?", (chat_id, session_id))
    row = cur.fetchone()
    if not row:
        return None
    questions = json.loads(row[0])
    current_q = int(row[1])
    scores = json.loads(row[2])
    start_ts = int(row[3])
    end_ts = int(row[4])
    return {'questions': questions, 'current_q': current_q, 'scores': scores, 'start_ts': start_ts, 'end_ts': end_ts}

def map_poll_to_q(chat_id: int, poll_id: str, session_id: str, q_index: int):
    cur = DB.cursor()
    cur.execute("INSERT INTO polls_map (chat_id, poll_id, session_id, q_index) VALUES (?,?,?,?)", (chat_id, poll_id, session_id, q_index))
    DB.commit()

def lookup_poll(poll_id: str):
    cur = DB.cursor()
    cur.execute("SELECT chat_id, session_id, q_index FROM polls_map WHERE poll_id=?", (poll_id,))
    row = cur.fetchone()
    if row:
        return {'chat_id': row[0], 'session_id': row[1], 'q_index': row[2]}
    return None

# --- Poll answer handler ---
@bot.poll_answer_handler(func=lambda m: True)
def handle_poll_answer(poll_answer):
    mapinfo = lookup_poll(poll_answer.poll_id)
    if not mapinfo:
        return
    chat_id = mapinfo['chat_id']
    session_id = mapinfo['session_id']
    q_index = mapinfo['q_index']
    session = load_session(chat_id, session_id)
    if not session:
        return
    questions = session['questions']
    scores = session['scores'] or {}
    selected_indices = poll_answer.option_ids
    user_id = poll_answer.user.id
    user_key = str(user_id)
    user_scores = scores.get(user_key, {"correct":0, "wrong":0, "answers":{}})
    correct_idx = questions[q_index].get("correct_index")
    chosen = selected_indices[0] if selected_indices else None
    user_answers = user_scores["answers"]
    user_answers[str(q_index)] = int(chosen) if chosen is not None else None
    if chosen is not None and correct_idx is not None and chosen == correct_idx:
        user_scores["correct"] += 1
    else:
        user_scores["wrong"] += 1
    user_scores["answers"] = user_answers
    scores[user_key] = user_scores
    save_session(chat_id, session_id, questions, current_q=session['current_q'], scores=scores, start_ts=session['start_ts'], end_ts=session['end_ts'])

# --- End quiz handler (called when timer finishes) ---
def end_quiz(session_id: str, chat_id: int):
    session = load_session(chat_id, session_id)
    if not session:
        return
    questions = session['questions']
    scores = session['scores'] or {}
    total_q = len(questions)
    # For each participant, send a DM with their results
    summary_lines = []
    for user_key, data in scores.items():
        try:
            uid = int(user_key)
        except:
            continue
        correct = data.get('correct',0)
        wrong = data.get('wrong',0)
        attempted = correct + wrong
        # send DM
        try:
            bot.send_message(uid, f"Quiz Finished! Your results:\\n✅ Correct: {correct}\\n❌ Wrong: {wrong}\\n📝 Attempted: {attempted}/{total_q}")
        except Exception:
            # user may have not started bot; ignore
            pass
        summary_lines.append(f"<a href='tg://user?id={uid}'>User</a>: ✅{correct} ❌{wrong} (attempted {attempted})")
    # Post group summary
    try:
        end_dt = datetime.utcfromtimestamp(session['end_ts']).strftime('%Y-%m-%d %H:%M:%S UTC')
    except:
        end_dt = "N/A"
    summary = "Quiz ended! Time: {}\n\nResults summary:\n".format(end_dt) + "\n".join(summary_lines) if summary_lines else "Quiz ended! No participants answered."
    try:
        bot.send_message(chat_id, summary, parse_mode='HTML')
    except Exception:
        pass
    # Optionally, mark session ended (set end_ts to past)
    save_session(chat_id, session_id, questions, current_q=session['current_q'], scores=scores, start_ts=session['start_ts'], end_ts=session['end_ts'])

# --- Commands ---
@bot.message_handler(commands=['start','help'])
def send_welcome(message):
    bot.send_message(message.chat.id, "MCQ→Quiz Bot ready. Use /quiz to start: send PDF with MCQs, then set quiz duration, and take the quiz.")

@bot.message_handler(commands=['quiz'])
def quiz_command(message):
    bot.send_message(message.chat.id, "Please upload the PDF containing MCQs (Gujarati/English). After upload I'll parse it and ask for quiz duration in minutes (e.g., 10).")

@bot.message_handler(commands=['solve'])
def cmd_solve(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /solve {question_number}")
        return
    try:
        q_no = int(parts[1]) - 1
    except:
        bot.reply_to(message, "Please provide a valid question number.")
        return
    # get latest session
    cur = DB.cursor()
    cur.execute("SELECT session_id FROM sessions WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (message.chat.id,))
    r = cur.fetchone()
    if not r:
        bot.reply_to(message, "No active session found.")
        return
    session_id = r[0]
    session = load_session(message.chat.id, session_id)
    if not session:
        bot.reply_to(message, "Session not found.")
        return
    questions = session['questions']
    if q_no < 0 or q_no >= len(questions):
        bot.reply_to(message, "Invalid question number.")
        return
    qobj = questions[q_no]
    best_idx, explanation = get_answer_with_gemini(qobj['question'], qobj['options'])
    letters = ['A','B','C','D','E']
    reply = f"Question {q_no+1}:\\n{qobj['question']}\\n\\nOptions:\\n"
    for i,opt in enumerate(qobj['options']):
        reply += f\"{letters[i]}. {opt}\\n\"
    reply += "\\nAI Explanation:\\n" + explanation
    bot.send_message(message.chat.id, reply)

# --- Document handler (PDF) ---
@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if not doc.file_name.lower().endswith(".pdf"):
        bot.reply_to(message, "Please upload a PDF file.")
        return
    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)
    if FORWARD_BACKUP_CHANNEL_ID:
        try:
            bot.forward_message(FORWARD_BACKUP_CHANNEL_ID, message.chat.id, message.message_id)
        except Exception:
            pass
    bot.reply_to(message, "PDF received. Parsing... This may take a few seconds.")
    try:
        text = extract_text_from_pdf_bytes(file_bytes)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error extracting text: {e}")
        return
    mcqs = parse_mcqs_from_text(text)
    if not mcqs:
        bot.send_message(message.chat.id, "No MCQs detected. Please send a clearly formatted MCQ PDF (questions numbered, options A/B/C/D).")
        return
    # Pre-compute LLM answers (optional, best-effort)
    for q in mcqs:
        idx, explanation = get_answer_with_gemini(q['question'], q['options'])
        q['correct_index'] = idx
        q['explanation'] = explanation
    # Save a session
    session_id = f"session_{int(time.time())}"
    save_session(message.chat.id, session_id, mcqs, current_q=0, scores={}, start_ts=0, end_ts=0)
    # Ask user to set quiz duration
    kb = types.InlineKeyboardMarkup()
    for m in [5,10,15,20,30,45,60]:
        kb.add(types.InlineKeyboardButton(text=f"{m} min", callback_data=f"setdur:{session_id}:{m}"))
    kb.add(types.InlineKeyboardButton(text="Custom", callback_data=f"setdur:{session_id}:custom"))
    bot.send_message(message.chat.id, f"Parsed {len(mcqs)} questions. Set quiz duration (minutes):", reply_markup=kb)

# --- Callback for duration selection ---
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("setdur:"))
def on_set_duration(call):
    try:
        _, session_id, val = call.data.split(":")
    except:
        bot.answer_callback_query(call.id, "Invalid data.")
        return
    if val == "custom":
        bot.answer_callback_query(call.id, "Please send the number of minutes (e.g., 20) as a message now.")
        # store a small marker in DB mapping from user->session awaiting custom value
        cur = DB.cursor()
        cur.execute("REPLACE INTO sessions (chat_id, session_id, questions_json, current_q, scores_json, start_ts, end_ts) VALUES (?,?,?,?,?,?,?)",
                    (call.message.chat.id, session_id, json.dumps(["__AWAITING_CUSTOM__"]), 0, json.dumps({}), 0, 0))
        DB.commit()
        return
    minutes = int(val)
    # Start quiz immediately by sending all polls; quiz will run for 'minutes'
    session = load_session(call.message.chat.id, session_id)
    if not session:
        bot.answer_callback_query(call.id, "Session not found.")
        return
    questions = session['questions']
    total_q = len(questions)
    # set start and end timestamps
    start_ts = int(time.time())
    end_ts = start_ts + minutes * 60
    save_session(call.message.chat.id, session_id, questions, current_q=0, scores={}, start_ts=start_ts, end_ts=end_ts)
    bot.answer_callback_query(call.id, f"Quiz will run for {minutes} minutes. Sending {total_q} questions now!")
    # send all polls immediately
    for idx, q in enumerate(questions):
        qtext = q.get('question') or "Question"
        options = q.get('options') or ["A","B","C","D"]
        try:
            poll = bot.send_poll(call.message.chat.id, qtext, options, is_anonymous=False)
            map_poll_to_q(call.message.chat.id, poll.poll.id, session_id, idx)
        except Exception as e:
            # fallback: send as message if poll can't be created
            bot.send_message(call.message.chat.id, f"Q{idx+1}: {qtext}\\nOptions:\\n" + "\\n".join([f"{i+1}. {o}" for i,o in enumerate(options)]))
        time.sleep(0.5)
    # send info message with end time
    end_dt = datetime.utcfromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M:%S UTC')
    info = bot.send_message(call.message.chat.id, f"Quiz started! It will end at {end_dt}. Use /result to check your score anytime. When time ends I'll post results automatically.")
    # schedule end_quiz
    delay = max(1, end_ts - int(time.time()))
    threading.Timer(delay, lambda: end_quiz(session_id, call.message.chat.id)).start()

# --- Handle custom duration value (simple message handler) ---
@bot.message_handler(func=lambda m: m.text and m.text.strip().isdigit())
def handle_custom_duration(message):
    # check if there's a session awaiting custom input for this chat
    cur = DB.cursor()
    cur.execute("SELECT session_id, questions_json FROM sessions WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (message.chat.id,))
    r = cur.fetchone()
    if not r:
        return
    session_id = r[0]
    questions_json = r[1]
    if questions_json and "__AWAITING_CUSTOM__" not in questions_json:
        # not awaiting custom
        return
    minutes = int(message.text.strip())
    if minutes <= 0 or minutes > 1440:
        bot.reply_to(message, "Please provide a reasonable duration in minutes (1-1440).")
        return
    # load original parsed questions from message history would be better; here we assume previous session stored real questions elsewhere
    # For safety, ask user to resend PDF if questions list missing
    # Try to find previous real session for this chat (skip awaiting marker)
    cur.execute("SELECT session_id, questions_json FROM sessions WHERE chat_id=? AND questions_json NOT LIKE ? ORDER BY rowid DESC LIMIT 1", (message.chat.id, "%__AWAITING_CUSTOM__%"))
    rr = cur.fetchone()
    if not rr:
        bot.reply_to(message, "Couldn't find parsed questions session. Please resend PDF.")
        return
    real_session_id = rr[0]
    session = load_session(message.chat.id, real_session_id)
    if not session:
        bot.reply_to(message, "Session missing. Please resend PDF.")
        return
    questions = session['questions']
    start_ts = int(time.time())
    end_ts = start_ts + minutes * 60
    save_session(message.chat.id, real_session_id, questions, current_q=0, scores={}, start_ts=start_ts, end_ts=end_ts)
    bot.reply_to(message, f"Quiz will run for {minutes} minutes. Sending {len(questions)} questions now!")
    for idx, q in enumerate(questions):
        qtext = q.get('question') or "Question"
        options = q.get('options') or ["A","B","C","D"]
        try:
            poll = bot.send_poll(message.chat.id, qtext, options, is_anonymous=False)
            map_poll_to_q(message.chat.id, poll.poll.id, real_session_id, idx)
        except Exception:
            bot.send_message(message.chat.id, f"Q{idx+1}: {qtext}\\nOptions:\\n" + "\\n".join([f"{i+1}. {o}" for i,o in enumerate(options)]))
        time.sleep(0.4)
    end_dt = datetime.utcfromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M:%S UTC')
    bot.send_message(message.chat.id, f"Quiz started and will end at {end_dt}.")
    delay = max(1, end_ts - int(time.time()))
    threading.Timer(delay, lambda: end_quiz(real_session_id, message.chat.id)).start()

# --- /result command to show user's current score ---
@bot.message_handler(commands=['result'])
def cmd_result(message):
    cur = DB.cursor()
    cur.execute("SELECT session_id FROM sessions WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (message.chat.id,))
    r = cur.fetchone()
    if not r:
        bot.reply_to(message, "No session found.")
        return
    session_id = r[0]
    session = load_session(message.chat.id, session_id)
    if not session:
        bot.reply_to(message, "Session not found.")
        return
    scores = session.get('scores', {})
    user_key = str(message.from_user.id)
    us = scores.get(user_key)
    if not us:
        bot.reply_to(message, "You have not answered any polls yet.")
        return
    correct = us.get('correct',0)
    wrong = us.get('wrong',0)
    attempted = correct + wrong
    total = len(session['questions'])
    start_ts = session.get('start_ts',0)
    end_ts = session.get('end_ts',0)
    time_taken = f"{int((min(int(time.time()), end_ts) - start_ts)/60)} minutes" if start_ts else "N/A"
    bot.send_message(message.chat.id, f"Your result so far:\\n✅ Correct: {correct}\\n❌ Wrong: {wrong}\\n📝 Attempted: {attempted}/{total}\\n⏱ Time taken: {time_taken}")

# --- Fallback ---
@bot.message_handler(func=lambda m: True)
def fallback(m):
    if m.text and m.text.strip().startswith("/"):
        bot.send_message(m.chat.id, "Unknown command. Use /quiz to start or send a PDF with MCQs.")
    else:
        bot.send_message(m.chat.id, "Send /quiz and then upload your MCQ PDF, or use /help.")

# --- Start polling ---
if __name__ == "__main__":
    print("Bot started.")
    bot.infinity_polling(timeout=120, long_polling_timeout=90)
