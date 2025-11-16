#!/usr/bin/env python3
# main.py - Telegram MCQ→Quiz Bot (Render Compatible / No OCR)

import os, re, io, time, json, threading, sqlite3
from datetime import datetime
from typing import List, Dict, Tuple, Optional

import telebot
from telebot import types

# PDF extract libs (safe for Render)
from PyPDF2 import PdfReader
from PIL import Image
import requests

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None
FORWARD_BACKUP_CHANNEL_ID = int(os.getenv("FORWARD_BACKUP_CHANNEL_ID", "0")) or None

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ---------------- DATABASE ----------------
DB_PATH = "quizbot.db"
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
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS polls_map (
  chat_id INTEGER,
  poll_id TEXT,
  session_id TEXT,
  q_index INTEGER
)
""")

conn.commit()

# ---------------- PDF → TEXT ----------------
def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """ Pure PyPDF2 extraction (OCR removed for Render compatibility). """
    pages = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""

    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except:
            txt = ""
        pages.append(txt.strip())

    return "\n\n".join(pages)

# ---------------- MCQ PARSER ----------------
OPTION_REGEX = re.compile(
    r'(?:\n|\r|^)\s*([A-Da-d1-4])[\).\-\s:]{1,3}\s*(.+?)(?=(?:\n\s*[A-Da-d1-4][\).\-\s:])|$)', re.S)

def parse_mcqs_from_text(text: str) -> List[Dict]:
    items = []
    if not text.strip():
        return items

    parts = re.split(r'\n(?=\s*\d{1,3}[\.\)\-]\s)', text)

    for part in parts:
        m = re.match(r'\s*(\d{1,3})[\.\)\-]\s*(.+)', part, re.S)
        if not m:
            continue

        num = int(m.group(1))
        rest = m.group(2).strip()
        opts = OPTION_REGEX.findall("\n" + rest)

        options = [o[1].strip().replace("\n", " ") for o in opts]

        qtext = rest
        if opts:
            first_opt = re.search(r'(?:\n|\r)\s*[A-Da-d1-4][\).\-\s:]{1,3}', rest)
            if first_opt:
                qtext = rest[:first_opt.start()].strip()

        if options:
            items.append({
                "num": num,
                "question": qtext,
                "options": options,
                "correct_index": None,
                "explanation": None
            })

    return items

# ---------------- AI (optional) ----------------
def get_answer_with_gemini(question: str, options: List[str]):
    if not GEMINI_API_KEY:
        return 0, "(Default: A)"
    return 0, "(Gemini logic not yet added)"

# ---------------- DB HELPERS ----------------
def save_session(chat_id, sid, questions, current_q=0, scores=None, start_ts=0, end_ts=0):
    cur.execute("""REPLACE INTO sessions 
    (chat_id, session_id, questions_json, current_q, scores_json, start_ts, end_ts)
    VALUES (?,?,?,?,?,?,?)""",
        (chat_id, sid, json.dumps(questions), current_q,
         json.dumps(scores or {}), start_ts, end_ts))
    conn.commit()

def load_session(chat_id, sid):
    cur.execute("SELECT * FROM sessions WHERE chat_id=? AND session_id=?", (chat_id, sid))
    r = cur.fetchone()
    if not r:
        return None
    return {
        "chat_id": r[0],
        "session_id": r[1],
        "questions": json.loads(r[2]),
        "current_q": r[3],
        "scores": json.loads(r[4]),
        "start_ts": r[5],
        "end_ts": r[6],
    }

def map_poll(chat_id, poll_id, sid, q_index):
    cur.execute("INSERT INTO polls_map VALUES (?,?,?,?)", (chat_id, poll_id, sid, q_index))
    conn.commit()

def lookup_poll(poll_id):
    cur.execute("SELECT chat_id, session_id, q_index FROM polls_map WHERE poll_id=?", (poll_id,))
    r = cur.fetchone()
    return {"chat_id": r[0], "session_id": r[1], "q_index": r[2]} if r else None

# ---------------- POLL ANSWERS ----------------
@bot.poll_answer_handler(func=lambda a: True)
def on_poll_answer(ans):
    ref = lookup_poll(ans.poll_id)
    if not ref:
        return

    sess = load_session(ref["chat_id"], ref["session_id"])
    if not sess:
        return

    uid = str(ans.user.id)
    chosen = ans.option_ids[0] if ans.option_ids else None

    scores = sess["scores"]
    if uid not in scores:
        scores[uid] = {"correct": 0, "wrong": 0, "answers": {}}

    correct_idx = sess["questions"][ref["q_index"]]["correct_index"]

    if chosen == correct_idx:
        scores[uid]["correct"] += 1
    else:
        scores[uid]["wrong"] += 1

    scores[uid]["answers"][str(ref["q_index"])] = chosen

    save_session(ref["chat_id"], ref["session_id"],
                 sess["questions"], sess["current_q"],
                 scores, sess["start_ts"], sess["end_ts"])

# ---------------- QUIZ END ----------------
def end_quiz(sid, chat_id):
    sess = load_session(chat_id, sid)
    if not sess:
        return

    scores = sess["scores"]
    results = []

    for uid, s in scores.items():
        msg = f"✅ {s['correct']} | ❌ {s['wrong']} | 📝 {s['correct'] + s['wrong']}"

        try:
            bot.send_message(int(uid), f"Your Quiz Result:\n{msg}")
        except:
            pass

        results.append(f"<a href='tg://user?id={uid}'>User</a>: {msg}")

    summary = "📢 Quiz Ended!\n\n" + "\n".join(results) if results else "Quiz ended! No answers."
    bot.send_message(chat_id, summary, parse_mode="HTML")

# ---------------- COMMANDS ----------------
@bot.message_handler(commands=["start", "help"])
def start_msg(m):
    bot.send_message(m.chat.id, "Send /quiz and upload your MCQ PDF!")

@bot.message_handler(commands=["quiz"])
def quiz_cmd(m):
    bot.send_message(m.chat.id, "📄 Send your MCQ PDF now.")

@bot.message_handler(content_types=["document"])
def on_doc(m):
    doc = m.document

    if not doc.file_name.lower().endswith(".pdf"):
        bot.reply_to(m, "❌ Only PDF allowed.")
        return

    file_info = bot.get_file(doc.file_id)
    pdf_bytes = bot.download_file(file_info.file_path)

    bot.reply_to(m, "📥 Extracting MCQs...")

    # extract text
    text = extract_text_from_pdf_bytes(pdf_bytes)
    mcqs = parse_mcqs_from_text(text)

    if not mcqs:
        bot.send_message(m.chat.id, "❌ No MCQs found.")
        return

    sid = f"session_{int(time.time())}"

    for q in mcqs:
        idx, exp = get_answer_with_gemini(q["question"], q["options"])
        q["correct_index"] = idx
        q["explanation"] = exp

    save_session(m.chat.id, sid, mcqs)

    kb = types.InlineKeyboardMarkup()
    for t in [5, 10, 15, 20, 30, 45, 60]:
        kb.add(types.InlineKeyboardButton(f"{t} min", callback_data=f"dur:{sid}:{t}"))

    bot.send_message(m.chat.id, f"Found {len(mcqs)} MCQs.\nSelect duration:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dur:"))
def on_duration(c):
    _, sid, mins = c.data.split(":")
    mins = int(mins)

    sess = load_session(c.message.chat.id, sid)

    start = int(time.time())
    end = start + mins * 60

    save_session(c.message.chat.id, sid, sess["questions"], 0, {}, start, end)

    bot.answer_callback_query(c.id, f"Starting {mins}-minute quiz!")

    # send all polls at once
    for i, q in enumerate(sess["questions"]):
        try:
            poll = bot.send_poll(
                c.message.chat.id,
                q["question"],
                q["options"],
                is_anonymous=False
            )
            map_poll(c.message.chat.id, poll.poll.id, sid, i)
        except:
            bot.send_message(c.message.chat.id, f"Q{i+1}: {q['question']}")
        time.sleep(0.4)

    threading.Timer(mins * 60, lambda: end_quiz(sid, c.message.chat.id)).start()

@bot.message_handler(commands=["result"])
def on_result(m):
    cur.execute(
        "SELECT session_id FROM sessions WHERE chat_id=? ORDER BY rowid DESC LIMIT 1",
        (m.chat.id,)
    )
    r = cur.fetchone()

    if not r:
        return bot.reply_to(m, "No quiz found.")

    sess = load_session(m.chat.id, r[0])
    scores = sess["scores"].get(str(m.from_user.id))

    if not scores:
        bot.reply_to(m, "You have no answers.")
        return

    bot.send_message(
        m.chat.id,
        f"✅ {scores['correct']} correct | ❌ {scores['wrong']} wrong"
    )

# ---------------- START BOT ----------------
if __name__ == "__main__":
    print("Bot running successfully on Render!")
    bot.infinity_polling(timeout=120, long_polling_timeout=90)