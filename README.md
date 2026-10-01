# Interview Mentor (MVP)

Terminal mentor: interviewer-style scenarios, 1-5 rubric debrief, quizzes, spaced re-quiz of gaps.

```bash
pip install anthropic pytest
export ANTHROPIC_API_KEY=...            # needed only for real sessions
python3 -m mentor.importer ../interview-notes.md   # (re)import scenarios; safe to re-run
python3 -m mentor                       # start a session; /quit to end
python3 -m pytest -q                    # tests run offline with a fake client
```

- Models: Sonnet 5.5 (interviewer/debrief), Haiku 4.5 (quiz questions). Edit `INTERVIEWER` / `QUIZ_MODEL` in `mentor/agent.py`.
- Budget: $10/month, tracked in the `usage` table; warns at 80%, refuses calls at 100% (`mentor/cost.py`).
- Data: `data/mentor.db` (SQLite).
