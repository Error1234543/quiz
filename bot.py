import os
import telebot
import fitz   # PyMuPDF for PDF text extraction
import requests
from flask import Flask, request
from datetime import datetime
import threading

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# ---------------------- MEMORY ----------------------
user_sessions = {}   # stores questions, answers, results, timer etc.


# ---------------------- GEMINI OCR ----------------------
def gemini_extract_text(image_bytes):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-vision:generateContent?key=" + GEMINI_API_KEY
    data = {
        "contents": [
            {
                "parts": [
                    {"text": "Extract all text clearly from this image"},
                    {"inline_data": {"mime_type": "image/png", "data": image_bytes}}
                ]
            }
        ]
    }
    r = requests.post(url, json=data)
    result = r.json()
    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return ""


# ---------------------- GEMINI ANSWER FINDER ----------------------
def gemini_answer(question, options):
    prompt = f"""
MCQ QUESTION:
{question}

OPTIONS:
A) {options.get('A')}
B) {options.get('B')}
C) {options.get('C')}
D) {options.get('D')}

Return only correct option letter (A/B/C/D).
"""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key=" + GEMINI_API_KEY
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(url, json=data)
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        for ch in ["A", "B", "C", "D"]:
            if ch in text:
                return ch
    except:
        pass
    return "A"


# ---------------------- PARSE MCQ OPTION-B FORMAT ----------------------
def parse_mcqs(text):
    lines = text.split("\n")
    questions = []
    q = ""
    opts = {}

    for line in lines:
        line = line.strip()

        if line == "":
            continue

        if line[0].isdigit() and ")" in line:
            if q != "":
                questions.append({"question": q, "options": opts})
            q = line
            opts = {}

        elif line.startswith("(A)") or line.startswith("A)"):
            opts["A"] = line.split(")", 1)[1].strip()

        elif line.startswith("(B)") or line.startswith("B)"):
            opts["B"] = line.split(")", 1)[1].strip()

        elif line.startswith("(C)") or line.startswith("C)"):
            opts["C"] = line.split(")", 1)[1].strip()

        elif line.startswith("(D)") or line.startswith("D)"):
            opts["D"] = line.split(")", 1)[1].strip()

    if q != "":
        questions.append({"question": q, "options": opts})

    return questions


# ---------------------- PDF HANDLER ----------------------
@bot.message_handler(content_types=['document'])
def pdf_handler(message):
    chat_id = message.chat.id

    file_id = message.document.file_id
    file = bot.get_file(file_id)
    pdf_bytes = bot.download_file(file.file_path)

    # SAVE TEMP PDF
    with open("temp.pdf", "wb") as f:
        f.write(pdf_bytes)

    bot.reply_to(message, "⏳ Extracting text from PDF... Wait 5–10 seconds")

    extracted_text = ""

    doc = fitz.open("temp.pdf")
    for page in doc:
        extracted_text += page.get_text()

    mcqs = parse_mcqs(extracted_text)

    if len(mcqs) == 0:
        bot.send_message(chat_id, "❌ No MCQs found. Make sure PDF is Option-B format.")
        return

    # STORE SESSION
    user_sessions[chat_id] = {
        "mcqs": mcqs,
        "answers": {},
        "correct_ans": {},
        "start_time": None,
        "end_time": None
    }

    # ASK TIME
    markup = telebot.types.InlineKeyboardMarkup()
    for t in ["5", "10", "30", "60", "90"]:
        markup.add(telebot.types.InlineKeyboardButton(f"{t} min", callback_data=f"time_{t}"))

    bot.send_message(chat_id, "⏳ Select Quiz Time:", reply_markup=markup)


# ---------------------- TIME SELECT ----------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("time_"))
def time_set(call):
    chat_id = call.message.chat.id
    minutes = int(call.data.split("_")[1])

    bot.edit_message_text(f"⏳ Quiz Time Set: {minutes} min\nQuiz starting...", chat_id, call.message.message_id)

    session = user_sessions.get(chat_id)
    session["start_time"] = datetime.now()

    # GENERATE AI ANSWERS FIRST
    bot.send_message(chat_id, "🤖 Finding answers using Gemini AI...")

    for i, q in enumerate(session["mcqs"]):
        ca = gemini_answer(q["question"], q["options"])
        session["correct_ans"][i] = ca

    bot.send_message(chat_id, "🔥 All answers ready! Sending full quiz...")

    # SEND ALL QUESTIONS
    for i, q in enumerate(session["mcqs"]):
        text = f"**Q{i+1}.** {q['question']}\n\n"
        text += f"A) {q['options']['A']}\n"
        text += f"B) {q['options']['B']}\n"
        text += f"C) {q['options']['C']}\n"
        text += f"D) {q['options']['D']}\n"

        markup = telebot.types.InlineKeyboardMarkup()
        for opt in ["A", "B", "C", "D"]:
            markup.add(telebot.types.InlineKeyboardButton(opt, callback_data=f"ans_{i}_{opt}"))

        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

    # START TIMER THREAD
    threading.Thread(target=quiz_timer, args=(chat_id, minutes)).start()


# ---------------------- USER ANSWERS ----------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("ans_"))
def handle_answer(call):
    chat_id = call.message.chat.id
    _, qnum, opt = call.data.split("_")
    qnum = int(qnum)

    user_sessions[chat_id]["answers"][qnum] = opt

    bot.answer_callback_query(call.id, f"Selected {opt}")


# ---------------------- TIMER END ----------------------
def quiz_timer(chat_id, minutes):
    import time
    time.sleep(minutes * 60)

    show_result(chat_id)


# ---------------------- RESULT FUNCTION ----------------------
def show_result(chat_id):
    session = user_sessions.get(chat_id)
    if not session:
        return

    session["end_time"] = datetime.now()

    correct = 0
    wrong = 0

    for i in range(len(session["mcqs"])):
        user_ans = session["answers"].get(i)
        real_ans = session["correct_ans"].get(i)

        if user_ans == real_ans:
            correct += 1
        else:
            wrong += 1

    total = len(session["mcqs"])
    taken = (session["end_time"] - session["start_time"]).seconds // 60

    result_msg = f"""
🎉 **QUIZ COMPLETED!**  
-------------------------
📊 **Result Summary**
Total Questions: {total}
✅ Correct: {correct}
❌ Wrong: {wrong}
⏱️ Time Taken: {taken} min
Accuracy: {round((correct/total)*100)}%
"""

    bot.send_message(chat_id, result_msg, parse_mode='Markdown')


# ---------------------- /solve COMMAND ----------------------
@bot.message_handler(commands=['solve'])
def solve_q(message):
    chat_id = message.chat.id
    parts = message.text.split()

    if len(parts) < 2:
        bot.reply_to(message, "Use: /solve 5")
        return

    num = int(parts[1]) - 1

    session = user_sessions.get(chat_id)
    if not session:
        bot.reply_to(message, "❌ No active quiz.")
        return

    q = session["mcqs"][num]
    prompt = f"""
Explain this MCQ in detail with correct answer.

Question:
{q['question']}

Options:
A) {q['options']['A']}
B) {q['options']['B']}
C) {q['options']['C']}
D) {q['options']['D']}
"""

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key=" + GEMINI_API_KEY
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(url, json=data)

    explanation = r.json()["candidates"][0]["content"]["parts"][0]["text"]

    bot.send_message(chat_id, explanation)


# ---------------------- FLASK WEBHOOK ----------------------
@app.route("/", methods=["POST"])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.stream.read().decode("utf-8"))
        bot.process_new_updates([update])
        return "OK"
    return "NOT JSON"


@app.route("/", methods=["GET"])
def hello():
    return "Bot Running"


# ---------------------- SET WEBHOOK ----------------------
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)

# ---------------------- RUN FLASK APP ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)