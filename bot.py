#!/usr/bin/env python3
# main.py - Telegram MCQ→Quiz Bot (PyPDF2 primary, optional OCR fallback)
# NOTE: For best OCR on scanned PDFs you need system tesseract + poppler (pdf2image).
# If those are not available on your host, scanned pages will be skipped.

import os, re, io, time, json, threading, sqlite3
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import telebot
from telebot import types

# PDF & OCR libraries (PyPDF2 preferred; pdf2image optional)
from PyPDF2 import PdfReader
from PIL import Image
import pytesseract
import requests

# Try optional pdf2image (needs poppler installed on system)
try:
    from pdf2image import convert_from_bytes
    PDF2IMAGE_AVAILABLE = True
except Exception:
    PDF2IMAGE_AVAILABLE = False

# ---- Config ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required (set in Render env vars).")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None
FORWARD_BACKUP_CHANNEL_ID = int(os.getenv("FORWARD_BACKUP_CHANNEL_ID", "0")) or None

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ---- Database ----
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

# ---- Utilities: PDF extraction ----
def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """
    Primary: extract text using PyPDF2.
    If page text is empty and pdf2image+pytesseract available, OCR the page.
    Returns concatenated text.
    """
    text_pages = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        return ""  # unreadable PDF

    # find pages with little or no text
    for p_index, page in enumerate(reader.pages):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        txt = txt.strip()
        if txt and len(txt) > 20:
            text_pages.append(txt)
        else:
            # try OCR only if pdf2image is available and tesseract likely present
            if PDF2IMAGE_AVAILABLE:
                try:
                    images = convert_from_bytes(pdf_bytes, first_page=p_index+1, last_page=p_index+1)
                    if images:
                        ocr_text = pytesseract.image_to_string(images[0], lang="eng+guj")
                        ocr_text = ocr_text.strip()
                        if ocr_text:
                            text_pages.append(ocr_text)
                        else:
                            text_pages.append("")  # empty OCR result
                    else:
                        text_pages.append("")
                except Exception:
                    # OCR attempt failed (likely missing poppler/tesseract), append placeholder
                    text_pages.append(f"[scanned page {p_index+1} - OCR unavailable on this host]")
            else:
                # no pdf2image -> cannot OCR here
                text_pages.append(f"[scanned page {p_index+1} - OCR not enabled]")
    return "\n\n".join(text_pages)

# --- MCQ parsing (heuristic) ---
OPTION_REGEX = re.compile(
    r'(?:\n|\r|^)\s*([A-DA-Da-d1-4])[\).\-\s:]{1,3}\s*(.+?)(?=(?:\n\s*[A-DA-Da-d1-4][\).\-\s:])|$)', re.S)

def parse_mcqs_from_text(text: str) -> List[Dict]:
    items = []
    if not text or text.strip() == "":
        return items
    parts = re.split(r'\n(?=\s*\d{1,3}[\.\)\-]\s)', text)
    for part in parts:
        m = re.match(r'\s*(\d{1,3})[\.\)\-]\s*(.+)', part, re.S)
        if not m:
            continue
        num = m.group(1)
        rest = m.group(2).strip()
        opts = OPTION_REGEX.findall("\n" + rest)
        options = [o[1].strip().replace("\n", " ") for o in opts]
        qtext = rest
        if opts:
            first_opt = re.search(r'(?:\n|\r)\s*[A-Da-d1-4][\).\-\s:]{1,3}', rest)
            if first_opt:
                qtext = rest[:first_opt.start()].strip()
        if options:
            items.append({"num": int(num), "question": qtext, "options": options,
                          "correct_index": None, "explanation": None})
    return items

# --- LLM placeholder ---
def get_answer_with_gemini(question: str, options: List[str]) -> Tuple[Optional[int], str]:
    if not GEMINI_API_KEY:
        return 0, "(No GEMINI_API_KEY set) default A"
    try:
        # Replace with your LLM call
        return 0, "(demo) replace with real Gemini/OpenAI logic"
    except Exception as e:
        return None, f"Error: {e}"

# ---- DB helpers (same as before) ----
def save_session(chat_id, sid, questions, current_q=0, scores=None, start_ts=0, end_ts=0):
    cur.execute("""REPLACE INTO sessions 
        (chat_id, session_id, questions_json, current_q, scores_json, start_ts, end_ts)
        VALUES (?,?,?,?,?,?,?)""",
        (chat_id, sid, json.dumps(questions), current_q, json.dumps(scores or {}), start_ts, end_ts))
    conn.commit()

def load_session(chat_id, sid):
    cur.execute("SELECT * FROM sessions WHERE chat_id=? AND session_id=?", (chat_id, sid))
    r = cur.fetchone()
    if not r: return None
    return {
        "chat_id": r[0], "session_id": r[1],
        "questions": json.loads(r[2]),
        "current_q": r[3], "scores": json.loads(r[4]),
        "start_ts": r[5], "end_ts": r[6]
    }

def map_poll(chat_id, poll_id, sid, q_index):
    cur.execute("INSERT INTO polls_map VALUES (?,?,?,?)", (chat_id, poll_id, sid, q_index))
    conn.commit()

def lookup_poll(poll_id):
    cur.execute("SELECT chat_id, session_id, q_index FROM polls_map WHERE poll_id=?", (poll_id,))
    r = cur.fetchone()
    return {"chat_id": r[0], "session_id": r[1], "q_index": r[2]} if r else None

# ---- Poll answer handler ----
@bot.poll_answer_handler(func=lambda a: True)
def on_poll_answer(ans):
    ref = lookup_poll(ans.poll_id)
    if not ref: return
    sess = load_session(ref["chat_id"], ref["session_id"])
    if not sess: return
    uid = str(ans.user.id)
    chosen = ans.option_ids[0] if ans.option_ids else None
    scores = sess["scores"]
    if uid not in scores: scores[uid] = {"correct": 0, "wrong": 0, "answers": {}}
    correct_idx = sess["questions"][ref["q_index"]].get("correct_index")
    if chosen == correct_idx:
        scores[uid]["correct"] += 1
    else:
        scores[uid]["wrong"] += 1
    scores[uid]["answers"][str(ref["q_index"])] = chosen
    save_session(ref["chat_id"], ref["session_id"], sess["questions"],
                 sess["current_q"], scores, sess["start_ts"], sess["end_ts"])

# ---- End Quiz ----
def end_quiz(sid, chat_id):
    sess = load_session(chat_id, sid)
    if not sess: return
    scores = sess["scores"]
    results = []
    for uid, s in scores.items():
        msg = f"✅ {s['correct']} | ❌ {s['wrong']} | 📝 {s['correct']+s['wrong']}"
        try:
            bot.send_message(int(uid), f"Your Quiz Result:\n{msg}")
        except: pass
        results.append(f"<a href='tg://user?id={uid}'>User</a>: {msg}")
    summary = "Quiz Ended!\n\n" + "\n".join(results) if results else "Quiz ended! No participants answered."
    bot.send_message(chat_id, summary, parse_mode="HTML")

# ---- Commands & handlers (same behavior) ----
@bot.message_handler(commands=["start", "help"])
def start_msg(m):
    bot.send_message(m.chat.id, "📘 Send /quiz and upload your MCQ PDF to start!")

@bot.message_handler(commands=["quiz"])
def quiz_cmd(m):
    bot.send_message(m.chat.id, "Send me your MCQ PDF. I'll convert it into a quiz.")

@bot.message_handler(content_types=["document"])
def on_doc(m):
    doc = m.document
    if not doc.file_name.lower().endswith(".pdf"):
        bot.reply_to(m, "Only PDF files supported.")
        return
    info = bot.get_file(doc.file_id)
    file = bot.download_file(info.file_path)
    bot.reply_to(m, "📄 Extracting MCQs...")
    text = extract_text_from_pdf_bytes(file)
    mcqs = parse_mcqs_from_text(text)
    if not mcqs:
        bot.send_message(m.chat.id, "No MCQs found. Check PDF formatting or OCR availability.")
        return
    sid = f"session_{int(time.time())}"
    for q in mcqs:
        idx, exp = get_answer_with_gemini(q["question"], q["options"])
        q["correct_index"], q["explanation"] = idx, exp
    save_session(m.chat.id, sid, mcqs)
    kb = types.InlineKeyboardMarkup()
    for t in [5,10,15,20,30,45,60]:
        kb.add(types.InlineKeyboardButton(f"{t} min", callback_data=f"dur:{sid}:{t}"))
    bot.send_message(m.chat.id, f"Parsed {len(mcqs)} MCQs.\nSet quiz duration:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dur:"))
def on_duration(c):
    _, sid, mins = c.data.split(":")
    mins = int(mins)
    sess = load_session(c.message.chat.id, sid)
    start = int(time.time())
    end = start + mins * 60
    save_session(c.message.chat.id, sid, sess["questions"], 0, {}, start, end)
    bot.answer_callback_query(c.id, f"Starting {mins}-minute quiz!")
    for i, q in enumerate(sess["questions"]):
        try:
            poll = bot.send_poll(c.message.chat.id, q["question"], q["options"], is_anonymous=False)
            map_poll(c.message.chat.id, poll.poll.id, sid, i)
        except:
            bot.send_message(c.message.chat.id, f"Q{i+1}: {q['question']}")
        time.sleep(0.4)
    threading.Timer(mins*60, lambda: end_quiz(sid, c.message.chat.id)).start()

@bot.message_handler(commands=["result"])
def on_result(m):
    cur.execute("SELECT session_id FROM sessions WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (m.chat.id,))
    r = cur.fetchone()
    if not r: return bot.reply_to(m, "No quiz found.")
    sess = load_session(m.chat.id, r[0])
    scores = sess["scores"].get(str(m.from_user.id))
    if not scores:
        bot.reply_to(m, "No answers yet.")
        return
    correct, wrong = scores["correct"], scores["wrong"]
    bot.send_message(m.chat.id, f"✅ {correct} correct | ❌ {wrong} wrong")

if __name__ == "__main__":
    print("✅ Bot running (PyPDF2 primary). OCR enabled:" , PDF2IMAGE_AVAILABLE)
    bot.infinity_polling(timeout=120, long_polling_timeout=90)