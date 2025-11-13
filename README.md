
# Telegram MCQ→Quiz Bot (Render-ready)

This project is a Telegram bot that converts MCQ PDFs into timed quizzes with polls.

## Features
- Upload a PDF containing MCQs (numbered questions with options A/B/C/D).
- Bot parses the PDF (text extraction + OCR fallback) and asks you to set quiz duration in minutes.
- After duration is set, the bot sends all polls (one per question) immediately and starts the quiz timer.
- When time ends, the bot automatically computes results for participants and DMs them their score; also posts a summary in the chat.
- `/solve {n}` gives an AI explanation for a question (LLM integration placeholder).

## Deployment (Render)
- Create a new service on Render as a **Background Worker**.
- Add environment variables:
  - `BOT_TOKEN` (required)
  - `GEMINI_API_KEY` (optional; required for AI explanations)
  - `OWNER_ID` (optional)
  - `FORWARD_BACKUP_CHANNEL_ID` (optional: -100...)
- Use the default start command.
- Ensure Tesseract is available on the deployment environment if you rely on OCR. (Render may not provide system-level tesseract by default; consider using images with clear text or pre-processing.)

## Notes
- The `get_answer_with_gemini()` function is a placeholder. Replace it with real API calls to your LLM provider.
- MCQ parsing is heuristic-based and may require PDFs to be in consistent formats (numbered questions and options labeled).
